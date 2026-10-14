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

通知（钉钉/企微/通用 webhook）是**旁路**（R-15 / GAP-3）：落库与 WS 广播先完成，
再对通知做**有界重试**（≤3 次、总耗时 ≤5s、指数退避），全失败则计数
`robot_alert_notify_failed_total{reason}`。通知失败**绝不**阻塞、回滚或推迟
告警落库 —— 外部系统故障不该让本系统丢数据。
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
from app.inhibition import (
    MIN_SOURCE_SEVERITY,
    ActiveAlertRegistry,
    InhibitionRecord,
    evaluate_severity_inhibition,
)
from app.escalation import escalation_loop
from app.silence import is_silenced
from app.rules.battery import BatteryLowRule
from app.rules.joint_stuck import JointStuckRule
from app.rules.motor_temp import MotorTempRule
from app.rules.offline import OfflineRule
from app.rules.base import Alert
from app import metrics
from app.notifiers import build_notifier
from app.notifiers.webhook import classify_exception
from app.store import load_online_state, save, save_suppressed, update_online

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

# 每台机器人当前活跃告警登记簿（订单 30）——severity 抑制的判据来源。
# 与 online_state 同源：进程内派生状态，见 app/inhibition.py 的说明。
active_alerts = ActiveAlertRegistry()

_tasks: set[asyncio.Task] = set()

# 通知器：未配置 URL 时为 None（整条链路静默跳过）。
# 在模块级装配属刻意的 fail-fast：kind 拼错时进程起不来，
# 好过配置写错却在运行时静默不通知。
_notifier = build_notifier(settings)

# ---- 通知重试上限（R-15 T3）------------------------------------------------
# 值是**硬上限**而非可调参数：需求 §6 明令「禁止无限重试 / 无上限等待」，
# 写进配置就给了"把它调成 100 次"的机会，那不是本单想要的自由度。
#   3 次：覆盖"限流一秒后自愈"这类瞬时故障；再多对群机器人无意义。
#   5s  ：与 `WebhookNotifier.timeout_s`（单次 HTTP 超时）同量级 —— 即最坏
#         情况下"一次尝试"的耗时上界，整体预算 = 3 × 单次预算，可算不失控。
# 指数退避基 0.5s → 0.5 / 1.0（仅两次间隔），总睡眠 1.5s，占预算的 30%。
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_TOTAL_BUDGET_S = 5.0
NOTIFY_BACKOFF_BASE_S = 0.5


def spawn(coro) -> None:
    """后台执行告警落库/发布，不阻塞消费循环；同时保留引用防止被 GC。"""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def inhibition_reason(alert: Alert) -> InhibitionRecord | None:
    """抑制判定，返回**可追溯凭证**；放行返回 None（订单 30：禁止静默丢弃）。

    两级判定，任一命中即抑制（附不同的 `reason` 以便审计归因）：

    1. **offline 层**（订单 10 既有）：机器人已判离线 → 抑制同机所有衍生告警。
       `reason="offline"`。
    2. **severity 层**（订单 30 新增）：同机存在更高 severity 的活跃告警 →
       抑制本条。`reason="severity"`。

    为什么把 offline 单列而不并入 severity 层：offline 是「整机失联」这一
    **绝对根因**，其语义是「任何衍生告警都无价值」；而 severity 层是相对压制。
    合并会让「离线时为何抑制 battery_low」与「高温时为何抑制低电量」的成因
    在指标上同形，运维无法区分是掉线还是过热。二者 `reason` 必须可分辨。

    两个关键不变量（写错会吞掉真实告警）：

    1. `online_state.get(rid) is False` —— 必须显式 `is False`，
       不能用 `not online_state.get(rid)`。ticker 要跑满一轮才会给 100 台机器人
       填值，启动初期字典为空，falsy 判定会把**所有**告警全判成被抑制。
    2. **抑制源自身永不被抑制** —— offline 规则不被 offline 层压；
       而 severity 层用 `exclude_rule` 排除自身，且要求 source 严格更高。
       否则根因告警永远报不出来。
    """
    rid = alert.robot_id

    # ---- 第 1 级：offline ----
    if settings.inhibit_offline and alert.rule_id != INHIBIT_SOURCE_RULE:
        if online_state.get(rid) is False:
            return InhibitionRecord(
                robot_id=rid,
                target_rule=alert.rule_id,
                target_severity=alert.severity,
                source_rule=INHIBIT_SOURCE_RULE,
                source_severity=3,
                reason="offline",
            )

    # ---- 第 2 级：severity（高等级抑制同机低等级）----
    if settings.inhibit_severity:
        rec = evaluate_severity_inhibition(
            alert, active_alerts, min_source_severity=MIN_SOURCE_SEVERITY
        )
        if rec is not None:
            return rec

    return None


def is_inhibited(alert: Alert) -> bool:
    """抑制判定的布尔门面（保持既有调用方/测试兼容）。

    实现委托给 `inhibition_reason()`；需要凭证时直接调后者。
    """
    return inhibition_reason(alert) is not None


# ---------------------------------------------------------------- 通知（旁路）

async def _send_with_retry(notifier, alert: Alert) -> tuple[bool, str | None]:
    """有界重试投递。返回 `(是否成功, 失败 reason)`。

    上限来自模块常量（见其注释）：**最多 `NOTIFY_MAX_ATTEMPTS` 次**、
    且**总耗时不超过 `NOTIFY_TOTAL_BUDGET_S`**。两个上限都要，
    因为单看次数不够 —— 每次 HTTP 自带 `timeout_s`，次数 × 单次超时
    就是最坏耗时；单看耗时不够 —— 预算用尽后要能给出"试了几次"。
    退避用指数（`base * 2**i`），且**每次睡眠前重算剩余预算**，
    保证 `asyncio.sleep` 不会把总耗时顶穿（"睡过头"是无限等待的温和版本，
    同样是需求 §6 禁止的）。
    """
    started = time.monotonic()
    last_reason: str | None = None

    for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
        try:
            if await notifier.send(alert):
                return True, None
        except Exception as exc:
            # 通知器契约是"永不外抛"（webhook.py:send 自带 try/except），
            # 这里再兜一层是**纵深防御**：只要有一次漏出去，异常会顺着
            # `spawn` 的任务冒到事件循环，把本单最核心的不变量
            # 「通知失败不影响落库/广播」破坏掉。代价是一行 except。
            log.exception("notifier raised despite contract: %s/%s",
                          alert.robot_id, alert.rule_id)
            last_reason = classify_notify_reason(exc)
        else:
            # 走 else 分支 = send() 正常返回 False（内部已吞异常），
            # reason 由通知器旁注在 `last_reason` 上（见 webhook.py 的说明）。
            last_reason = getattr(notifier, "last_reason", None) or "unknown"

        if attempt == NOTIFY_MAX_ATTEMPTS:
            break

        backoff = NOTIFY_BACKOFF_BASE_S * (2 ** (attempt - 1))
        remaining = NOTIFY_TOTAL_BUDGET_S - (time.monotonic() - started)
        if remaining <= backoff:
            # 预算不足以再等一轮退避 → 放弃重试，直接用当前 reason 计数。
            # 这不是"提前失败"，而是"再等就超预算"，故 break 而非 sleep(0)。
            log.warning("notify retry budget exhausted after %d attempt(s) on %s/%s",
                        attempt, alert.robot_id, alert.rule_id)
            break
        await asyncio.sleep(backoff)

    return False, last_reason


def classify_notify_reason(exc: BaseException) -> str:
    """把通知器意外抛出的异常归类。

    正常路径上 `webhook.py:send()` 不会外抛，reason 由通知器自己给出；
    本函数只服务 `_send_with_retry` 的兜底分支 —— 但**不能省略**：
    兜底分支也必须产出合法标签，否则 `metrics.notify_failed.labels(reason=...)`
    会被塞进异常类名之类的高基数取值。委托给通知器的同一张分类表
    （`classify_exception`），保证"谁分类"只有一处实现。
    """
    return classify_exception(exc)


async def _notify(alert: Alert) -> None:
    """通知**旁路任务**：投递 + 计数，失败只记指标/日志，永不外抛。

    为什么是 TODO 里的"后台任务"而不是在 `emit()` 里 `await`（关键决策）：
      `emit()` 跑在 `spawn()` 出来的消费任务里，而它的**下一件事**就是回去
      `pubsub.listen()` 取下一条消息。若在这里 await 通知（哪怕是有界重试），
      最坏 5s 内该协程无法取消息 —— 100 台 @10Hz 的流会在 Redis 缓冲里积压，
      而"通知慢"（外部系统）就这样变成了"告警消费慢"（本系统）。
      本单的硬门槛是**通知失败不得阻塞告警落库/广播**；把通知整体挪出主链路，
      是把这条门槛从"尽量"变成"结构上不可能违反"。
      代价：一条告警的通知与它的落库不再严格同一时刻完成，可接受 ——
      通知本就是"尽力而为"（需求 §3）。
    """
    # 在 await 之前取快照：重试期间 `_notifier` 可能被替换（测试会 monkeypatch），
    # 本任务应始终对"当时的那个通知器"负责。
    notifier = _notifier
    if notifier is None:
        return
    try:
        ok, reason = await _send_with_retry(notifier, alert)
    except asyncio.CancelledError:
        raise                     # 取消是控制流，不是投递失败，不得计成失败
    except Exception as exc:      # pragma: no cover - 纵深防御，正常不可达
        log.exception("notify task crashed: %s/%s", alert.robot_id, alert.rule_id)
        ok, reason = False, classify_notify_reason(exc)

    if ok:
        metrics.notify_sent.labels(kind=getattr(notifier, "kind", "unknown")).inc()
        return

    # 失败：计数必须落在**封闭 reason 集合**内（`NotifyError` 已做归一，
    # 这里再兜一次 None/空串，防止标签出现空值）。
    metrics.notify_failed.labels(reason=reason or "unknown").inc()
    # 失败已由通知器逐次 `log.exception` 打出，这里补一行**结论级**日志：
    # 记明"最终放弃了、重试了几次"，否则日志里只有 3 条同样的失败栈，
    # 读者无法区分"失败但重试成功"与"彻底失败"（指标能区分，人肉看日志不能）。
    log.error("notify giving up after retries: reason=%s %s/%s",
              reason or "unknown", alert.robot_id, alert.rule_id)


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
    # 先问布尔门面（保持既有 monkeypatch 接缝），命中再取凭证。
    # 两步而非直接 `inhibition_reason()`：`is_inhibited` 是订单 10/17/24
    # 测试与外部约定的稳定接缝，直接绕过它会让既有 spy 失效。
    if is_inhibited(alert):
        record = inhibition_reason(alert)
        # 抑制计数必须单独埋：否则 `alerts_emitted` 的**下降**无法归因 ——
        # 到底是机器人真的健康了，还是被抑制层挡掉了。二者运维含义完全不同。
        # 订单 30 追加 `reason` 维度：offline 与 severity 的运维动作不同
        # （前者查网络/电源，后者查根因告警是否已处置）。
        # record 可能为 None：仅当 `is_inhibited` 被外部 monkeypatch 成恒真、
        # 而真实判定放行时才会发生（测试接缝场景）。此时降级为 reason="unknown"，
        # 不掩盖调用，也不抛异常中断告警链路。
        reason = record.reason if record is not None else "unknown"
        metrics.alerts_suppressed.labels(
            rule_id=alert.rule_id, reason=reason
        ).inc()
        # 禁止静默丢弃：DEBUG 是给"能拿到日志的人"，运维在 INFO 级/告警台
        # 看不到 DEBUG。故抑制凭证用 WARNING 单行输出，含 source 与 target，
        # 可直接 grep `inhibition` 追出"这条为什么没出去"。
        log.warning(
            "alert inhibited: %s",
            record.as_log_fields() if record is not None
            else f"reason=unknown robot={alert.robot_id} target={alert.rule_id}(sev{alert.severity})",
        )
        # 订单 35：把抑制凭证**落库**（不推 WS、不通知），使 `GET /alerts` 可查。
        # 订单 30 的"不静默丢弃"此前只到日志与指标为止 —— 告警台看不到被压制的行，
        # 值班员得换到日志系统才能回答"这台机器当时还报了什么"。
        # 旁路语义：落库失败只记指标+日志（见 store.save_suppressed），不影响主链路；
        # 与 `save()` 一样 spawn 成后台任务，不阻塞消费线。
        if record is not None:
            spawn(save_suppressed(alert, record))
        return
    window = DEDUP_WINDOW.get(alert.rule_id, 60)
    if await should_emit(alert, window):
        await save(alert)
        # 登记为活跃告警（订单 30）：本条已发出，即成为同机后续低等级告警的
        # 抑制源候选。放在 save 之后 —— 只有真的发出去的告警才配当根因。
        active_alerts.register(alert)
        metrics.alerts_emitted.labels(rule_id=alert.rule_id).inc()
        log.info("alert: %s %s sev=%d %s", alert.robot_id, alert.rule_id,
                 alert.severity, alert.message)
        # 推到工厂侧群机器人（钉钉/企微/通用）。
        # 放在 save 之后：先落库保证审计，再通知。被抑制的告警在上面已 return，
        # 不会走到这里。
        #
        # R-15（GAP-3）：由 `await _notifier.send(...)` 改为 `spawn(_notify(...))`，
        # 理由见 `_notify` 的 docstring —— 通知链路上的等待（含重试）绝不允许
        # 占用告警消费循环。失败在 `_notify` 内终结（只计数 + 日志），
        # 结构上不可能回滚或阻塞上面已完成的 `save()` / 广播。
        if _notifier is not None:
            spawn(_notify(alert))


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
            else:
                # 本轮该规则未命中 = 已恢复 → 清登记，否则它会永远当"根因"
                # 压制同机的其它告警（订单 30 关键点：不及时清除 = 永久误抑制）。
                active_alerts.clear(rid, rule.rule_id)


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
            else:
                # 未再判离线 = 已恢复 → 清 offline 登记（否则它作为 sev3 抑制源
                # 会一直压低同机的告警）。订单 30。
                active_alerts.clear(rid, INHIBIT_SOURCE_RULE)

            online = ls is not None and (now - ls) <= settings.offline_after_s
            if online_state.get(rid) is not online:
                online_state[rid] = online
                spawn(update_online(rid, online))
                if not online:
                    # 判离线即清该机**衍生**告警登记：此刻它们已无处置价值，
                    # 留着只会与 offline 的抑制语义重叠，污染 severity 层的判据。
                    # offline 自身的登记由上面的 emit 在抑制通过后写入。
                    active_alerts.clear_robot(rid)

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
