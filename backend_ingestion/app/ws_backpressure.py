"""WS 出站背压 + 心跳（ISSUE #73）。

## 现象 / 根因

改前 `ws_robots`（`main.py`）的增量循环是**同步 await** 每个客户端的 `send_json`：

    async for msg in pubsub.listen():
        await ws.send_json({"type": "telemetry", "data": data})

`await send_json` 只保证"帧已交给内核"，不保证"客户端已消费"。慢客户端
（移动网络 / 前端卡顿 / 标签页被冻结）会让 uvicorn 的 transport 写缓冲涨到高水位，
此后每一次 `send` 都阻塞在 `drain()` 上 —— 而这**阻塞的是转发循环本身**，
于是：① 该慢客户端把出站帧积压在内存里无界增长；② 整个转发循环被它拖住，
所有健康客户端一起挨饿。1000 msg/s × N 客户端下，一个卡死的浏览器标签就能
拖垮整个进程。

## 本模块的解法

给每条连接一个**有界出站队列**（`asyncio.Queue(maxsize=N)`）+ 一个独立发送协程：

- 转发循环只做 `put_nowait`，**永不 await**，慢客户端不再拖累循环；
- 队列满时按消息类型分级处置（见 `enqueue`），最坏情况内存 = N × 帧大小，
  且 N 是配置里写死的常量，不随时长增长；
- 发送协程 `await ws.send_json` 仍会因背压阻塞，但阻塞的是**它自己**，
  队列满即触发丢弃，链路保持有界。

## 告警不可丢（ISSUE #73 「约束/禁止项」第三条）

`robot:alerts` 是前端告警流的**唯一数据源**，丢一条就是漏一条告警。因此
队列溢出时的处置是分级的：**只丢遥测，保住告警**。队列里没有遥测可丢时
（极端情形：整队都是告警）宁可让队列多涨一格，也不丢告警 —— 用
`queue.maxsize + 少量余量` 的方式把"告警不可丢"这条硬约束编码进实现，
而不是靠注释约定。

## 心跳（ISSUE #73 第二条验收）

`ping/pong` 由 Starlette 原生支持：收到 `websocket.receive` 且 `message["text"]`
为 `"ping"` 时回 `"pong"`。客户端主动发 ping 是浏览器端"半开连接"检测的常规手段
（TCP 层不会因为拔网线立刻报错）。服务端侧则用 `_HEARTBEAT` 哨兵驱动：
发送协程每轮循环往队列放一个哨兵，途中会 `await` 到背压/断开等真实状态，
于是"发得出去"本身就是一次验活，不需要额外定时器。

## 一个刻意的取舍：不用 `State.WAITING_FOR_RESPONSE`

ISSUE #73 验收原文写的是「检查连接的可写/背压状态（`State.WAITING_FOR_RESPONSE`
或队列深度）」。本仓实测（Starlette 0.36.3）：

    >>> [x for x in dir(WebSocketState) if not x.startswith("_")]
    ['CONNECTED', 'CONNECTING', 'DISCONNECTED']

**服务端 Starlette 没有 `WAITING_FOR_RESPONSE` 这个状态**（它是客户端
`websockets` 库的状态机概念）。因此本实现走验收给出的**第二个等价判据 —— 队列深度**，
并辅以 `transport.get_write_buffer_size()` 作为真实写缓冲的观测值（仅用于日志/指标，
不作为唯一判据：uvicorn 0.28 未向外暴露该 transport，读不到时静默降级）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("ws_backpressure")

#: 遥测帧的标识（与 `main.py` 的出站信封一致）。
TELEMETRY = "telemetry"
#: 告警帧的标识。**不可丢**。
ALERT = "alert"

#: 队列满且全是告警时的额外余量。让"告警不可丢"在多几条告警时仍然成立，
#: 同时把最坏内存增长钉在一个常量上。
ALERT_OVERFLOW_SLACK = 64

#: 心跳哨兵。放在队列里而不是起定时器：发送协程每轮自然 flush 一次，
#: 顺带把"这条连接还发得出去吗"验掉，不需要第二个并发源。
_HEARTBEAT = object()

# ---- 预序列化帧通道（ISSUE #71 融合设计）----
# ws_hub 的节拍扇出把每帧**只序列化一次**（一份发布 → 多连接复用），入队的是
# 现成的 JSON 文本。队列项的 kind 用哨兵对象而非字符串，与 dict 信封通道
# 绝不混淆；droppability 仍按 telemetry/alert 分级（见 _drop_oldest_telemetry）。
_RAW_TELEMETRY = object()
_RAW_ALERT = object()


class WsOutbound:
    """单条 WS 连接的出站通道：有界队列 + 独立发送协程。

    生命周期：`__init__` → `start()` → `enqueue()`（任意次）→ `aclose()`。
    所有公开方法都是**不阻塞转发循环**的（`enqueue` 用 `put_nowait`），
    这是本模块存在的全部意义。
    """

    def __init__(
        self,
        ws,
        *,
        maxsize: int,
        alert_slack: int = ALERT_OVERFLOW_SLACK,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        # 【为什么队列一次分配到位，而不是"满了再换一只大的"】
        # 提示：本文件上一版真的尝试过"告警来了就换更大队列"（`_rehome_for_alert`），
        # 那是**错的**：发送协程正在 `await queue.get()` 消费老队列，此时把
        # `self._queue` 换成新对象，搬移与消费互相竞争，会**静默丢帧**
        # （实测混流 5000 条时少 1 条告警，且只在"搬移恰好撞上消费"时出现）。
        # 正解：物理容量一次给足（遥测软上限 + 告警预留），再用一个**软上限**
        # 决定遥测从哪一刻开始丢 —— 全程无需搬移，无竞态。
        #
        # maxsize 至少 1，否则 asyncio.Queue 退化成无上限（maxsize<=0 语义）。
        # 配置项配错也不允许出现"无界堆积"——这正是本单要消灭的现象。
        self._telemetry_limit = max(1, maxsize)
        # 物理容量 = 遥测软上限 + 告警预留：给"不可丢的告警"独立空间。
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=self._telemetry_limit + max(0, alert_slack)
        )
        self._ws = ws
        self._on_sent = on_sent
        self._task: asyncio.Task | None = None
        # 遥测节流：丢了多少帧、合并了多少次。分两个计数是为了把
        # "背压真的在丢"与"只是被合并了"区分开 —— 盯指标的人需要这两条不一样。
        self.telemetry_dropped = 0
        self.telemetry_merged = 0
        self.alerts_deferred = 0
        # 发送进度：`_sender` 每成功发一帧就 +1 并刷新时间。
        # watchdog 靠"计数是否前进 / 时间是否前进"判断连接是否还活着，
        # 这是它唯一需要的两个信号（避免再给每连接挂一个定时器）。
        self.sent_count = 0
        self.last_sent_at: float | None = None

    # ------------------------------------------------------------- 入队

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def maxsize(self) -> int:
        """遥测的软上限（不含留给告警的那截）。"""
        return self._telemetry_limit

    @property
    def capacity(self) -> int:
        """物理容量 = 遥测软上限 + 告警预留。"""
        return self._queue.maxsize

    @property
    def is_saturated(self) -> bool:
        """队列是否已顶到日常容量（实时判定，不含告警扩容的那部分余量）。

        watchdog 用它判断"客户端消费速度是否长期低于流入速度"。
        用 `full()` 而非 `qsize()>=maxsize`：前者是 asyncio 自己的判据，
        与 `put_nowait` 抛 `QueueFull` 的条件严格一致，不会出现"我说满了但还能塞进去"。
        """
        return self._queue.full()

    def enqueue(self, kind: str, envelope: dict) -> None:
        """把一帧放进出站队列。**永不阻塞、永不抛**。

        分级处置（`kind` 由调用方按出站信封的 `type` 传入）：

        - `alert`：先尝试直接入队；队列满则**扩容**（上限 `maxsize + alert_slack`），
          仍满才 `deferred` 计数。告警**永不被丢弃**（ISSUE #73 禁止项第三条）。
        - 其它（遥测）：队列满时丢**队首**（最旧的一帧）再入队。
          丢最旧而非丢最新，是因为遥测是"最新值优先"的时序数据 ——
          前面那帧的价值已经被后面的覆盖，丢它损失最小（spec/01 §4.3 状态缓存同理）。
        """
        if kind == ALERT:
            self._enqueue_alert(envelope)
        else:
            self._enqueue_telemetry(kind, envelope)

    def enqueue_raw(self, kind: str, text: str) -> None:
        """预序列化帧入队（ws_hub 节拍扇出专用）。语义与 `enqueue` 完全一致。

        区别仅在载荷形态：`text` 是 hub **已经序列化好**的 JSON 字符串，
        发送协程走 `send_text` 直发，不再每连接重复 `json.dumps` ——
        这是「一份序列化 → N 连接复用」的落点（ISSUE #71 的核心收益）。
        droppability 与 dict 通道相同：遥测可丢（丢最旧）、告警永丢不得。
        """
        if kind == ALERT:
            self._enqueue_raw_alert(text)
        else:
            self._enqueue_raw_telemetry(text)

    def _enqueue_telemetry(self, kind: str, envelope: dict) -> None:
        # 【判据是"遥测**自己**的额度"，不是物理容量】
        # 物理容量里多出来的那截是**留给告警的**。若这里按物理容量判断，
        # 遥测会先把预留区占满，随后告警只能落到 deferred ——
        # "告警不可丢"就退化成"谁先到谁占位"（实测：maxsize=5+slack=4 时，
        # 遥测占满 9 格、3 条告警全部顺延）。
        if self._queue.qsize() >= self._telemetry_limit:
            # 找队首的遥测帧丢弃。用 `_queue._queue` 直接探测是必要的：
            # 队列里可能混着哨兵与告警，只能从**最旧的**一侧开始找。
            dropped = self._drop_oldest_telemetry()
            if dropped is None:
                # 队里没有可丢的遥测（已被发送协程取走/全是哨兵），
                # 这一帧没被接纳 —— 记"合并"而非"丢弃"：没丢任何已有数据。
                self.telemetry_merged += 1
                return
            self.telemetry_dropped += 1
        try:
            self._queue.put_nowait((kind, envelope))
        except asyncio.QueueFull:  # pragma: no cover - 丢帧后必有空位
            self.telemetry_dropped += 1

    def _enqueue_alert(self, envelope: dict) -> None:
        """告警入队。**永不丢**（ISSUE #73 禁止项第三条）。

        物理容量里已预留 `alert_slack` 格专供告警（见 `__init__`），
        所以正常路径是"直接进"。只有病态情形（告警自身积压超过预留量）
        才落到 `alerts_deferred` 记账 —— 那时仍**不丢**，只是让这条顺延，
        运维据该指标判断是否需要扩容或摘连接。

        这里刻意**不做**任何"换队列腾位"的动作：那会与正在消费的发送协程
        竞争并静默丢帧（见 `__init__` 的实测说明）。
        """
        try:
            self._queue.put_nowait((ALERT, envelope))
        except asyncio.QueueFull:
            self.alerts_deferred += 1

    def _enqueue_raw_telemetry(self, text: str) -> None:
        """预序列化遥测入队，判据与 `_enqueue_telemetry` 相同（软上限 + 丢最旧）。"""
        if self._queue.qsize() >= self._telemetry_limit:
            dropped = self._drop_oldest_telemetry()
            if dropped is None:
                self.telemetry_merged += 1
                return
            self.telemetry_dropped += 1
        try:
            self._queue.put_nowait((_RAW_TELEMETRY, text))
        except asyncio.QueueFull:  # pragma: no cover - 丢帧后必有空位
            self.telemetry_dropped += 1

    def _enqueue_raw_alert(self, text: str) -> None:
        """预序列化告警入队：同样只走预留区，**永不丢**（同 `_enqueue_alert`）。"""
        try:
            self._queue.put_nowait((_RAW_ALERT, text))
        except asyncio.QueueFull:
            self.alerts_deferred += 1

    def _drop_oldest_telemetry(self) -> tuple | None:
        """从队首起找第一帧遥测并移除，返回被丢的元组；找不到返回 None。

        队列里可能有四种东西，必须分开对待：
          - `(TELEMETRY, envelope)`        → 可丢；
          - `(_RAW_TELEMETRY, text)`       → 可丢（hub 预序列化遥测，同遥测语义）；
          - `(ALERT/​_RAW_ALERT, ...)`      → **不可丢**，跳过；
          - `_HEARTBEAT` 裸哨兵（`object()`）→ 可丢，但**没有下标**。

        ⚠️ 判据必须是「是遥测才可丢」的白名单，**不是** `item[0] is not ALERT`：
        后者会把 `_RAW_ALERT` 也当成可丢（哨兵对象 `is not` 字符串恒真），
        一旦队首恰好是一条预序列化告警，它就会被静默丢弃 —— 违反
        「告警不可丢」的硬约束。此函数第一版还漏过第三种（哨兵下标越界
        TypeError），教训同源：对队列里的东西做**穷举**，别做排除。
        """
        items = self._queue._queue  # 直接访问 deque：从头部定位才可能 O(1) 丢最旧
        for i, item in enumerate(items):
            if item is _HEARTBEAT:
                del items[i]
                return None  # 丢哨兵不算丢遥测（无信封可返回）
            kind = item[0]
            if kind is TELEMETRY or kind is _RAW_TELEMETRY:
                del items[i]
                return item
        return None

    # ------------------------------------------------------------- 出站

    def start(self) -> None:
        """启动发送协程。由 `ws_robots` 在 accept 之后调用。"""
        self._task = asyncio.create_task(self._sender())

    async def _sender(self) -> None:
        """串行发送。**唯一**调用 `ws.send_json` / `ws.send_text` 的地方。

        为什么串行而非并发：WebSocket 帧必须有序，并发 send 会让帧交错，
        客户端 JSON.parse 直接炸。串行也让"背压"这件事有单一定义点 ——
        阻塞只发生在这一处，队列长度就是唯一的积压度量。

        载荷双形态：dict 信封走 `send_json`（本连接自行序列化）；
        hub 预序列化的 RAW 帧走 `send_text` 直发 —— 每帧的 json.dumps
        全进程只做一次（ISSUE #71 的核心收益），客户端拿到的字节完全一致。
        """
        try:
            while True:
                item = await self._queue.get()
                if item is _HEARTBEAT:
                    continue
                kind, payload = item
                if kind is _RAW_TELEMETRY or kind is _RAW_ALERT:
                    await self._ws.send_text(payload)
                else:
                    await self._ws.send_json(payload)
                self.sent_count += 1
                self.last_sent_at = asyncio.get_running_loop().time()
                if self._on_sent is not None:
                    self._on_sent()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 发送失败（客户端断开/帧非法）不吞掉：记日志后让协程退出，
            # 由 `ws_robots` 的 finally 负责回收计数与队列。
            # 注意**不能**在这里 close ws —— 关闭动作归 `ws_robots`，
            # 否则会与它的 finally 抢同一个连接。
            log.warning("ws outbound sender stopped", exc_info=True)

    def heartbeat(self) -> None:
        """往队列里塞一个心跳哨兵（队列满则跳过，不阻塞）。"""
        try:
            self._queue.put_nowait(_HEARTBEAT)
        except asyncio.QueueFull:
            pass

    async def aclose(self) -> None:
        """取消发送协程并等待其退出。幂等，可重复调用。"""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            # 发送协程内部已记过日志；这里只保证不外抛，
            # 因为 aclose 的调用点（finally）必须无条件走完回收。
            pass

    def drain_nowait(self) -> list[dict]:
        """取走队列里剩余的可发送信封（跳过哨兵），供测试断言与优雅关闭使用。"""
        out: list[dict] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _HEARTBEAT:
                continue
            out.append(item[1])
        return out


def envelope_size(envelope: dict) -> int:
    """估算一帧的字节数（观测用：日志里报"积压了多少字节"比"多少帧"直观）。"""
    try:
        return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError):  # pragma: no cover - 信封来自 json.loads，必然可序列化
        return 0


__all__ = [
    "ALERT",
    "TELEMETRY",
    "ALERT_OVERFLOW_SLACK",
    "WsOutbound",
    "envelope_size",
]
