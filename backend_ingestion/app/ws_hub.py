"""WS 出站 fan-out 中枢（ISSUE #71）—— 一份订阅、按节拍、每机器人只留最新一帧。

【为什么需要它（问题根因）】
改造前 `main.py` 的 `ws_robots` 是「每连接一套实现」：
  ① 每个 WebSocket 各自 `r.pubsub()` 订阅 `robot:stream:all` + `robot:alerts`；
  ② 对收到的**每一条**遥测调用一次 `await ws.send_json(...)`。
规模 = 100 台 × 10Hz = 1000 msg/s。N 个大屏连着时，服务端要做
`N × 1000` 次/秒的 JSON 序列化 + 发送，Redis 侧同一条消息也被 N 个订阅者各取一次
—— CPU/内存随客户端数**线性膨胀**，与"大屏到底开着几台"这个本应稳定的信号耦合。

【本模块的解法（与 ISSUE #73 背压层的融合设计）】
把「订阅」「合并」与「投递」拆开，各自只做一份：
  1. **共享订阅**：全进程**一个** pubsub 订阅 `chan_stream_all` 与 `chan_alerts`
     （替代每连接独立订阅）；
  2. **节点合并**：遥测按 `robot_id` 覆盖写进「最新一帧」字典 `_latest` ——
     每台机器人任一时刻只有一帧，中间帧被自然去重（coalesced_total 计数）；
  3. **固定节拍扇出**：独立的 `_broadcast_loop` 每 `ws_broadcast_interval_ms`
     （默认 100ms = 10Hz）把 `_latest` 里**每台机器人最后一条**序列化**一次**，
     再把同一个字符串经 `outbound.enqueue_raw()` 投进**每条连接自己的**
     WsOutbound 有界队列（#73 的背压层）—— 由各连接的发送协程串行 `send_text`。

为什么节拍只 `put_nowait`、不直接 `await ws.send_text`：
中枢若直发，一个慢客户端就会把**广播节拍本身**阻塞住，所有健康连接一起挨饿
—— 这恰是 #73 在单连接尺度上消灭过的病灶，换到进程尺度会复发。
put_nowait 永不阻塞：慢客户端只把自己队列塞满、只丢自己的旧遥测，
告警照走预留区（不可丢），节拍时长与任何客户端的快慢**解耦**。

于是服务端的订阅数从 N 份降到 **1 份**，序列化从 N × 1000/s 降到
**1000/s 全进程一份**（字符串在连接间复用），发送仍由 #73 的有界队列兜底。
每台机器人对外仍是 10Hz，信封契约逐字段不变。

【为什么遥测按 id 合并、告警不合并（契约面）】
spec/00 §5.2 的 telemetry 信封是**单台** `{"type":"telemetry","data":{一台}}`，
前端 `useRobotWebSocket.ts` 的 `latestDataRef.set(robot_id, item)` 本身就是
"同 id 覆盖"语义 —— 服务端提前做同一件事，**对外契约逐字段不变**。
告警是**事件流**（前端 `pushAlert` 逐条 prepend，时间序有意义），
既不能合并成数组（那会改信封），也不能丢弃，故经同一节拍逐条原样转发 ——
`chan_alerts` 的转发是前端告警流的唯一数据源，**禁止移除**（ISSUE #71 禁止项）。

【禁止项自查（对齐 ISSUE #71「约束 / 禁止项」）】
- 不去掉 `chan_alerts` 转发：见 `_consume_loop` 的 alert 分支（同一共享订阅里转发）；
- 不破坏鉴权顺序：鉴权与 1008 拒绝仍留在 `ws_robots` 里，本模块只在**注册之后**
  才可能发帧；
- 不引入每条消息的额外 DB 查询：本模块只碰 Redis pubsub 与内存字典，**零 DB 调用**。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app import metrics
from app.config import settings
from app.db.redis import get_redis
from app.ws_backpressure import WsOutbound

log = logging.getLogger("ws_hub")


def _envelope(kind: str, data: Any) -> str:
    """构造 spec/00 §5.2 的统一信封。

    `separators` 去掉空格：1000 帧/s 下每个逗号/冒号的空格都是真实的字节开销。
    `ensure_ascii=False` 让告警中文 message 按 UTF-8 直出（Redis pubsub 里
    本来就是 UTF-8 bytes，这一层不该做任何改写）。
    """
    return json.dumps({"type": kind, "data": data}, separators=(",", ":"), ensure_ascii=False)


class WsHub:
    """进程级单例：共享订阅 + 节点合并 + 固定节拍 fan-out（经每连接背压队列）。"""

    def __init__(self) -> None:
        # 注册的是**每条连接的出站通道**（WsOutbound），不是裸 WebSocket：
        # 中枢只做 enqueue_raw（put_nowait），把"慢客户端只拖垮自己"交给 #73。
        self._clients: list[WsOutbound] = []
        # robot_id -> **最新一帧**遥测（值已是还原类型后的 dict，见 state_cache._decode）。
        self._latest: dict[str, dict] = {}
        # 告警事件缓冲：不合并、不丢弃，等下一个节拍逐条原样转发。
        self._alerts: list[dict] = []
        # 【刻意不加 asyncio.Lock】所有临界区（快照+清空 / 覆盖写 / 注册表增删）
        # 内部**没有任何 await**，在单线程事件循环上天然原子；uvicorn 多 worker
        # 是多进程（各持本单例一份），不存在共享内存竞争。加了锁反而引入
        # asyncio 原语的跨事件循环绑定问题：本单例活过测试套件的多个
        # TestClient 会话（每个会话新循环），Lock 首次 acquire 绑死旧循环后，
        # 新会话一碰就是 RuntimeError —— BatchWriter._drained 的同款毒。
        # 同理 start() 每次都重建两个任务（见下），stop() 清空容器。
        self._task: asyncio.Task | None = None
        self._redis_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 只读快照（指标用）

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def pending_robots(self) -> int:
        """当前待扇出的机器人数（= 上一节拍后累计到的最新帧数）。"""
        return len(self._latest)

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动共享订阅与节拍广播两个任务。重复调用是幂等的。"""
        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._broadcast_loop(), name="ws-hub-broadcast")
        self._redis_task = loop.create_task(self._consume_loop(), name="ws-hub-consume")
        log.info(
            "ws hub started: interval=%.0fms channel=%s alerts=%s",
            settings.ws_broadcast_interval_ms,
            settings.chan_stream_all,
            settings.chan_alerts,
        )

    async def stop(self) -> None:
        """取消两个后台任务并清空缓冲（shutdown 用）。"""
        for task in (self._task, self._redis_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾不得抛
                pass
        self._task = None
        self._redis_task = None
        self._clients.clear()
        self._latest.clear()
        self._alerts.clear()

    # ------------------------------------------------------------ 连接注册

    async def register(self, outbound: WsOutbound) -> None:
        self._clients.append(outbound)

    async def unregister(self, outbound: WsOutbound) -> None:
        """连接回收时（ws_robots 的 finally）注销。重复注销不抛，静默即可。"""
        try:
            self._clients.remove(outbound)
        except ValueError:
            pass

    # ------------------------------------------------------------ 扇出

    async def broadcast_now(self, interval_s: float) -> None:
        """把当前合并结果投给所有连接的出站队列（一个节拍的具体动作）。

        **每个连接不单独做合并，也不在这里序列化第二遍**：`_latest` 与
        `_alerts` 快照并清空（同步段，天然原子，见 __init__ 注释），随后
        **一次性**序列化成帧字符串，所有连接经 `enqueue_raw` 复用同一批
        字符串 —— 一份发布 → N 连接复用。扇出全程只有 `put_nowait`，
        **没有任何 await 在客户端上**：慢客户端最多让自己的队列满，
        节拍本身不受任何客户端快慢影响（#73 融合）。
        """
        latest = list(self._latest.values())
        alerts = list(self._alerts)
        self._latest.clear()
        self._alerts.clear()
        clients = list(self._clients)

        if not clients or (not latest and not alerts):
            return

        # 一次性序列化：telemetry 逐台各一次、alert 逐条各一次，
        # 之后 N 个连接复用同一个字符串对象。
        frames = [(False, _envelope("telemetry", item)) for item in latest]
        frames.extend((True, _envelope("alert", alert)) for alert in alerts)
        n_telem = len(latest)
        n_alert = len(alerts)

        for ob in clients:
            for is_alert, text in frames:
                ob.enqueue_raw("alert" if is_alert else "telemetry", text)

        # 指标：实际发送量与「被合并掉的量」。
        # frames 是**每个连接各一份**，故乘上连接数 —— 这正是要监控的量：
        # 改造前它 ≈ clients × 1000/s，改造后 ≈ clients × (节拍 × 机器人)，
        # 而 coalesced 的绝对值直接证明「每机器人只留最新一帧」在起作用。
        sent = len(clients)
        metrics.ws_hub_frames_total.labels(kind="telemetry").inc(n_telem * sent)
        metrics.ws_hub_frames_total.labels(kind="alert").inc(n_alert * sent)

        log.debug(
            "ws hub tick: robots=%d alerts=%d clients=%d interval=%.3fs",
            n_telem,
            n_alert,
            sent,
            interval_s,
        )

    async def _broadcast_loop(self) -> None:
        interval = settings.ws_broadcast_interval_ms / 1000.0
        if interval <= 0:
            interval = 0.1
        # 固定节拍：interval 只取决于配置，**不随消息数变化** ——
        # 这正是验收要求的「按固定节拍推送」，也是压测里 CPU 不随大屏数线性增长的前提。
        while True:
            await asyncio.sleep(interval)
            try:
                await self.broadcast_now(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一个节拍异常不得让整个广播循环退出（否则所有大屏从此收不到帧）。
                log.exception("ws hub broadcast tick failed")

    # ------------------------------------------------------------ 共享订阅（一份）

    async def _consume_loop(self) -> None:
        """全进程**唯一**的 Redis pubsub 消费循环。

        与改造前的关键差别：订阅只建**一次**，而不是每个连接一次。
        断线（Redis 抖动）时退避重连 —— 没有它，一次 Redis 重连就会让所有
        大屏永久静默（而连接本身还开着，表现为"假绿"）。
        """
        delay = 1.0
        while True:
            pubsub = None
            try:
                r = await get_redis()
                pubsub = r.pubsub()
                await pubsub.subscribe(settings.chan_stream_all, settings.chan_alerts)
                delay = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    channel = msg["channel"]
                    if channel == settings.chan_alerts:
                        self._alerts.append(data)
                    elif channel == settings.chan_stream_all:
                        rid = data.get("robot_id") if isinstance(data, dict) else None
                        if rid:
                            # 覆盖写 = 每台机器人只留最新一帧（本地化收敛）。
                            # 计数：**这一条如果覆盖了同 id 的未扇出帧，就是被合并掉的**。
                            if rid in self._latest:
                                metrics.ws_hub_coalesced_total.inc()
                            self._latest[rid] = data
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("ws hub redis loop error, reconnect in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(settings.chan_stream_all, settings.chan_alerts)
                        await pubsub.aclose()
                    except Exception:
                        pass


# 进程级单例。main.py 的 lifespan 负责 start()/stop()；
# ws_robots 直接 import 使用（与 `writer = BatchWriter()` 同一模式）。
hub = WsHub()
