"""告警引擎入口（spec/02 §5）。

两条驱动线：
1. **消费线**：订阅 Redis `robot:stream:all`，维护每台机器人滑动窗口，
   依次跑 motor_temp / battery_low / joint_stuck 三条规则。
2. **ticker 线**：每秒遍历 100 台机器人跑 offline 规则。
   判据**优先读** `robot_metadata.is_online`（后端 metadata_writer 心跳写入，ISSUE #96）；
   该列读不到时回落到内存 last_seen 时间差，避免部署初期误报。
   —— **不依赖 Redis Key 过期事件**（keyspace notification 默认关闭）。

告警先经**三层过滤**，顺序固定为 **silence → inhibition → dedup**
（理由见 `emit()` 的 docstring），再由 store 写入 robot_alert 并 PUBLISH robot:alerts。

另有一条 **escalation 线**（订单 17 T2）：周期性扫描"severity=3 且超时未 ack"
的告警，标记 escalated 并重发，对应 Alertmanager 的 `repeat_interval` 语义。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

from app.config import settings
from app.db import close_all, get_redis
from app.deduplicator import should_emit
from app.escalation import escalation_loop
from app.silence import is_silenced
from app.rules.battery import BatteryLowRule
from app.rules.joint_stuck import JointStuckRule
from app.rules.motor_temp import MotorTempRule
from app.rules.offline import OfflineRule
from app.rules.base import Alert
from app import metrics
from app.notifiers import build_notifier
from app.store import load_online_state, save, update_online

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("alert_engine")

RULES = [MotorTempRule(), BatteryLowRule(), JointStuckRule(), OfflineRule()]
DEDUP_WINDOW = {r.rule_id: r.dedup_window_s for r in RULES}
STREAM_RULES = [r for r in RULES if r.rule_id != "offline"]
OFFLINE_RULE = next(r for r in RULES if r.rule_id == "offline")

# 抑制的**源规则**：offline 告警活跃时，抑制同一机器人的其他告警。
# 对应 Alertmanager 的 `inhibit_rules`：source=offline / equal=[robot_id]。
INHIBIT_SOURCE_RULE = "offline"

windows: dict[str, deque] = {}
# last_seen 仍由消费线维护，作为 offline 的**回落**判据（ISSUE #96：列不可用时）
# 以及告警文案（"已 Ns 无遥测"）。
last_seen: dict[str, float] = {}
# 本引擎按内存 last_seen 推算的在线态，仍按状态变化低频写 robot_metadata
# （兼容 #61 既有路径）；同时供 inhibition（is_inhibited）使用。
online_state: dict[str, bool] = {}

_tasks: set[asyncio.Task] = set()

# 通知器：未配置 URL 时为 None（整条链路静默跳过）。
# 在模块级装配属刻意的 fail-fast：kind 拼错时进程起不来，
# 好过配置写错却在运行时静默不通知。
_notifier = build_notifier(settings)


def spawn(coro) -> None:
    """后台执行告警落库/发布，不阻塞消费循环；同时保留引用防止被 GC。"""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def is_inhibited(alert: Alert) -> bool:
    """抑制判定（inhibition）—— 必须在 emit() 里去重**之前**执行。

    语义对齐 Alertmanager `inhibit_rules`：
        source: rule_id == "offline"（severity 3）
        target: 同一 robot_id 的其他所有规则
        equal:  [robot_id]

    两个关键点（写错会吞掉真实告警）：

    1. `online_state.get(rid) is False` —— 必须显式 `is False`，
       不能用 `not online_state.get(rid)`。ticker 要跑满一轮才会给 100 台机器人
       填值，启动初期字典为空，falsy 判定会把**所有**告警全判成被抑制。
    2. offline 规则**自身永不被抑制** —— 否则高严重度的根因告警永远报不出来。
    """
    if not settings.inhibit_offline:
        return False
    if alert.rule_id == INHIBIT_SOURCE_RULE:
        return False
    return online_state.get(alert.robot_id) is False


async def emit(alert: Alert) -> None:
    """告警出口。三层过滤，**顺序不可调换**：silence → inhibition → dedup。

    为什么必须是这个顺序（订单 17 T3 要求写清理由）：

    1. **silence 最先**。静默是运维显式下达的"此刻不要打扰我"指令，
       优先级高于一切自动判定。若放在 inhibition/dedup 之后，会带来两个后果：
       - 被静默的告警仍会**写 dedup Key**，于是静默解除后的第一秒内，
         本该报出的告警被"静默期内留下的去重窗口"二次压制 —— 静默产生了
         它在语义上不该有的后效。
       - 静默的意图是"不处理"，而 inhibition/dedup 都需要先读状态（Redis/内存），
         多一次无谓的判定。
    2. **inhibition 次之**。抑制是"根因已知，衍生告警无价值"（机器人离线时，
       电量低/关节卡死不必报）。它必须在 dedup **之前**，否则衍生告警会先
       占掉 dedup 窗口，等根因恢复后真实告警可能被去重挡掉 —— 订单 10 的
       原设计即如此（`emit` 里 inhibition 在 `should_emit` 之前），本单保留。
    3. **dedup 最后**。去重窗口是一次"计数副作用"（写 Redis Key），
       只有确定这条告警真的要发出去，才配消耗窗口。前两层都不该浪费它。

    换言之：silence 决定"要不要看"，inhibition 决定"这条有没有价值"，
    dedup 决定"这条本窗口内发过没有"。由粗到细，每层只处理"上一层放行"的告警。
    """
    if await is_silenced(alert):
        # 静默计数单独埋点：与抑制同理，`alerts_emitted` 的下降必须可归因
        # —— 是静默挡掉的，还是抑制挡掉的，运维动作完全不同。
        metrics.alerts_silenced.labels(rule_id=alert.rule_id).inc()
        log.debug("alert silenced: %s %s (silence key active)", alert.robot_id,
                  alert.rule_id)
        return
    if is_inhibited(alert):
        # 抑制计数必须单独埋：否则 `alerts_emitted` 的**下降**无法归因 ——
        # 到底是机器人真的健康了，还是被抑制层挡掉了。二者运维含义完全不同。
        metrics.alerts_suppressed.labels(rule_id=alert.rule_id).inc()
        log.debug("alert inhibited: %s %s (robot offline)", alert.robot_id,
                  alert.rule_id)
        return
    window = DEDUP_WINDOW.get(alert.rule_id, 60)
    if await should_emit(alert, window):
        await save(alert)
        metrics.alerts_emitted.labels(rule_id=alert.rule_id).inc()
        log.info("alert: %s %s sev=%d %s", alert.robot_id, alert.rule_id,
                 alert.severity, alert.message)
        # 推到工厂侧群机器人（钉钉/企微/通用）。
        # 放在 save 之后：先落库保证审计，再通知；通知失败只在通知器内部记日志，
        # 不影响主链路。被抑制的告警在上面已 return，不会走到这里。
        if _notifier is not None:
            await _notifier.send(alert)


# ---------------------------------------------------------------- 消费线

async def consume() -> None:
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(settings.chan_stream_all)
    log.info("alert_engine consuming %s", settings.chan_stream_all)
    async for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        try:
            data = json.loads(msg["data"])
        except json.JSONDecodeError:
            continue
        rid = data.get("robot_id")
        if not rid:
            continue

        w = windows.setdefault(rid, deque(maxlen=settings.window_maxlen))
        w.append(data)
        last_seen[rid] = time.time()

        for rule in STREAM_RULES:
            try:
                alert = rule.evaluate(data, w)
            except Exception:
                log.exception("rule %s failed on %s", rule.rule_id, rid)
                continue
            if alert is not None:
                spawn(emit(alert))


# ---------------------------------------------------------------- ticker 线

async def ticker() -> None:
    """每秒一轮：跑 offline 规则（判据优先读 robot_metadata.is_online）+ 维护在线状态。

    为什么先读一次库（ISSUE #96）
    -----------------------------
    判据必须是「后端看到过这台机器人吗」，而不是「本进程内存里记的 last_seen」。
    后者会在本引擎重启 / Redis pubsub 断连时悄悄失真（内存被清空 → 100 台集体
    判离线）。故每轮先 `load_online_state()` 取回 100 行，再逐台判 offline：
    每轮 **1 次**库往返，不按台数放大。

    读不到（返回 {}）→ 传给规则的 `is_online` 是 None → 规则回落到 last_seen
    时间差判定（迁移期安全网，见 `app/rules/offline.py`）。
    """
    while True:
        await asyncio.sleep(settings.tick_interval_s)
        now = time.time()
        db_state = await load_online_state()
        for rid in settings.robot_ids:
            ls = last_seen.get(rid)
            w = windows.get(rid) or deque()
            is_online = db_state.get(rid)      # 缺行 / 读库失败 → None（回落）
            try:
                alert = OFFLINE_RULE.evaluate(
                    {"robot_id": rid, "is_online": is_online, "last_seen": ls}, w
                )
            except Exception:
                log.exception("offline rule failed on %s", rid)
                alert = None
            if alert is not None:
                spawn(emit(alert))

            online = ls is not None and (now - ls) <= settings.offline_after_s
            if online_state.get(rid) is not online:
                online_state[rid] = online
                spawn(update_online(rid, online))

        # 拉模型下 Gauge 必须在每轮刷新：没人替我们定期 set，
        # 不刷新则抓到的永远是"上一次状态变化那一刻"的过期值。
        metrics.robots_online.set(sum(1 for v in online_state.values() if v))


async def amain() -> None:
    try:
        # escalation_loop 与另两条驱动线并发：它不对应"输入流"，而是周期性
        # 扫描"已发出、severity=3、超时仍未 ack"的告警，重发升级（订单 17 T2）。
        await asyncio.gather(consume(), ticker(), escalation_loop())
    finally:
        for t in list(_tasks):
            t.cancel()
        await asyncio.gather(*_tasks, return_exceptions=True)
        await close_all()


def main() -> None:
    # 先起 metrics HTTP 服务（独立线程，标准库 http.server），
    # 保证 Prometheus 无论何时抓取都拿得到 up=1，而不是等 asyncio 循环先跑起来。
    # 本服务不是 FastAPI，没有路由可挂 /metrics，故只能独立起端口。
    server = metrics.start(settings.metrics_port)
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("alert_engine stopped")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
