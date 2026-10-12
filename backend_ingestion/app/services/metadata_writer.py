"""robot_metadata 心跳批量维护（G-6 / ISSUE #96）—— is_online / last_seen 的唯一写入方。

为什么必须是「批量 + 独立通道」
--------------------------------
100 台 × 10Hz = **1000 msg/s**。若每条遥测都发一次
`UPDATE robot_metadata SET last_seen=now() ...`，等于把 1000 次单行 UPDATE/s
灌进一台同时还在吞 1000 行 COPY 的库上——这正是 AGENTS.md §3 明令禁止的
「逐条 UPDATE」。故本模块沿两条护栏走：

1. **按 robot_id 折叠**：metadata 表的主键是 `robot_id`，同一台机器人在一个刷新
   窗口内无论来了 1 条还是 100 条遥测，语义上只对应一行。因此在内存里用
   `pending: dict[str, float]` 折叠，1000 行/s 最终只折算成 **≤100 行/窗口**。
2. **一条语句写完一批**：`UPDATE ... FROM unnest($1::text[], $2::timestamptz[])`
   一次往返落一整批（1000 msg/s 下约 1 次往返/秒）。不是 executemany，
   也不是 for 循环逐条 UPDATE。

为什么独立于 `batch_writer.queue`
---------------------------------
`batch_writer` 的队列元素类型是 `Telemetry`，且其批量写路径与
`robot_telemetry` 的 COPY 列清单强绑定（`COLUMNS` / `_to_row`）。把 metadata
心跳塞进去要么给 Telemetry 加字段，要么在刷盘路径里判类型分叉——前者污染
数据契约，后者把两条无关的写路径耦死。故心跳走 `Telemetry` 之外的**旁路队列**，
与 `writer.queue` 用同一套「生产者 `put` / 单消费者 `run`」范式，但仍是
asyncio.Queue 批量消费 + 单语句写库，不产生 1000 次/s 的写放大。

与 main 上 `liveness.py` 的关系（ISSUE #77 / #96）
--------------------------------------------------
#77 引入的 `liveness.py` 把**热路径**（1000 msg/s）的接收侧事实放在 Redis，
并明文写着「`robot_metadata.is_online/last_seen` 由告警引擎按状态变化低频落库」。
那套做法在 100 台规模下有真实缺陷：告警引擎只能看到**它自己收到的** `robot:stream:all`，
一旦它重启 / Redis pubsub 断连，#77 的注释里承诺的「状态变化」就永远不会发生，
而看板不会报错（它读的是后端 identity 视图现算的在线数）—— 列停在旧值没人看见。

本模块把写方收成**唯一**一处、且放在**收到遥测的那条链路上**（`state_cache`），
于是「DB 里的 is_online 落后于 Redis」这件事变成结构上不可能，而不是靠定时器兜。
写入仍是批量折叠（1000 msg/s → ≤100 行/1s），不违反 AGENTS.md §3。

last_seen 用**服务端时刻**而不是 payload 里的 `t.ts`
----------------------------------------------------
`t.ts` 是模拟器/机器人侧的时间戳，可被伪造、可漂移；`robot_metadata.last_seen`
问的是「**服务端**最后一次看到这台机器人是什么时候」，判据必须是本机时钟。

⚠️ 这不是语义洁癖，是实测出来的故障：若把 payload 的 `ts` 当 `last_seen` 写库，
只要客户端时钟落后服务器 / 重放历史数据 / 离线缓发，`MARK_OFFLINE_SQL` 的
`last_seen < NOW() - ttl` 谓词会在**同一轮刷新**里把刚写成 TRUE 的行立刻翻回 FALSE
（实测「5 台刚上报、紧接一句全被判离线」）。故 `put()` 只取 `time.time()`。

副作用是同一窗口内多台机器人拿到同一个 `now()`——这与语义一致（它们确实在
同一时刻被看到），不像 `robot_telemetry.time` 那样需要毫秒级区分。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.db.session import get_pool

log = logging.getLogger("metadata_writer")

# 单语句批量 upsert。两个关键点：
# - `IS DISTINCT FROM` 做条件更新：心跳值每轮都在变，但 `(is_online, last_seen)`
#   才是这行数据的**状态**。把它写进 WHERE 让「值没变就不写」，省掉绝大多数
#   无效行更新，同时不改变语义（状态真变了才落盘）。
# - 用 `unnest` 而非拼接 VALUES：绑定数组走扩展协议，没有 SQL 文本拼接，
#   也天然免疫注入。
UPDATE_SQL = """
UPDATE robot_metadata AS m
SET last_seen = v.last_seen, is_online = TRUE
FROM unnest($1::text[], $2::timestamptz[]) AS v(robot_id, last_seen)
WHERE m.robot_id = v.robot_id
  AND (m.last_seen IS DISTINCT FROM v.last_seen OR m.is_online IS DISTINCT FROM TRUE)
"""

# 周期置离线。**必须带 `is_online = TRUE`**：否则每轮都会把 100 台全表改写一遍
# （含本来就没上过线的），制造无意义的 WAL 与膨胀。
# `last_seen` 谓词走 timestamptz 时间差（与服务器时区无关）。
MARK_OFFLINE_SQL = """
UPDATE robot_metadata
SET is_online = FALSE
WHERE is_online = TRUE
  AND (last_seen IS NULL OR last_seen < NOW() - make_interval(secs => $1))
RETURNING robot_id
"""


class MetadataWriter:
    """robot_metadata 心跳的批量写方（生产者 `put` / 消费者 `run`）。"""

    def __init__(self) -> None:
        # stop() 用它提前唤醒 run() 的等待。
        self._stop_event = asyncio.Event()
        # 无界队列：折叠后的写入量是「不同的 robot_id」个数（ids 是 100 个固定
        # 字符串，dict 的键不增长），生产者侧不存在无界堆积，故不需要 maxsize 背压。
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self.flushes = 0            # 落库的刷新轮数
        self.rows_written = 0       # 实际被 UPDATE 命中的行数（已去重的机器人台数）
        self.offline_marks = 0      # 累计置离线的次数
        self.dupes: dict[str, int] = {}  # robot_id → 被折叠掉的重复条数（观测用）
        self._stop = False

    # ---- 生产者：每条遥测只做一次 dict 赋值，不做 IO ----
    def put(self, robot_id: str, ts_ms: int | None = None) -> None:
        """投递一台机器人的心跳。

        **`last_seen` 取服务端时钟 `time.time()`，不取 payload 的 `ts_ms`。**

        这一点踩过实证坑：把 payload 的 `ts` 当 `last_seen` 写库时，只要客户端的
        时钟与服务器有偏差（或重放历史数据、离线缓发），`MARK_OFFLINE_SQL` 的
        `last_seen < NOW() - ttl` 谓词会在**同一轮刷新里**把它刚写成 TRUE 的行
        立刻翻回 FALSE —— 实测「5 台刚上报、下一句就被判离线」。`last_seen` 回答的
        是「**服务端**最后一次看到这台机器人」，判据只能是本机时钟。

        `ts_ms` 形参保留只是为了让调用点自文档化（`state_cache` 传了 `t.ts`），
        当前实现**刻意忽略**它的值。
        """
        del ts_ms          # 见 docstring：不做算术，避免把客户端时钟引进判据
        self.queue.put_nowait((robot_id, time.time()))

    # ---- 消费者 ----
    async def run(self) -> None:
        if not settings.metadata_heartbeat_enabled:
            # 开关关闭：不落库、不扫描，直接退出（任务自灭，lifespan 无需特判）。
            log.info("metadata_writer disabled by config (METADATA_HEARTBEAT_ENABLED=false)")
            return
        log.info("metadata_writer started (trigger=%.2fs, offline>%ss)",
                 settings.metadata_flush_seconds, settings.state_ttl_seconds)
        while True:
            await self._sleep(settings.metadata_flush_seconds)
            await self.flush()
            if self._stop:
                return

    async def _sleep(self, seconds: float) -> None:
        """可被 stop() 提前唤醒的 sleep（关闭时不必干等一个刷新周期）。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def flush(self) -> None:
        """折叠 + 一条语句落库 + 顺手周期置离线。异常在此消化，不拖垮采集循环。"""
        if not settings.metadata_heartbeat_enabled:
            return
        pending: dict[str, float] = {}
        while not self.queue.empty():
            rid, ts = self.queue.get_nowait()
            # 后到的覆盖先到的即可：我们只关心「最后一次被看到的时刻」，
            # 同一窗口内多条遥测不需要取 max（模拟器按序发送）。
            if rid in pending:
                self.dupes[rid] = self.dupes.get(rid, 0) + 1
            pending[rid] = ts
        try:
            await self._write(pending)
        except Exception:
            # 与 batch_writer 的取舍一致：宁可丢心跳历史，也不能让采集停摆。
            log.exception("metadata heartbeat flush failed, %d robots dropped", len(pending))

    async def _write(self, pending: dict[str, float]) -> None:
        if pending:
            ids = list(pending.keys())
            seen = [datetime.fromtimestamp(ts, tz=timezone.utc) for ts in pending.values()]
            pool = await get_pool()
            async with pool.acquire() as conn:
                status = await conn.execute(UPDATE_SQL, ids, seen)
            try:
                self.rows_written += int(status.split()[-1])
            except (IndexError, ValueError):    # 状态串格式意外变化时不误报
                self.rows_written += len(ids)

        # 离线扫描每轮都跑：机器人停发后队列是空的，只扫「有遥测的那批」等于
        # 永远不会把停发的机器人置 false。
        pool = await get_pool()
        async with pool.acquire() as conn:
            offline = await conn.fetch(MARK_OFFLINE_SQL, float(settings.state_ttl_seconds))
        if offline:
            self.offline_marks += len(offline)
            log.info("robots marked offline (>%ss no telemetry): %s",
                     settings.state_ttl_seconds,
                     ", ".join(r["robot_id"] for r in offline[:5])
                     + (" ..." if len(offline) > 5 else ""))
        self.flushes += 1

    async def stop(self) -> None:
        """置位并唤醒 run()，把队列存量落库后返回（关闭顺序见 main.py lifespan）。"""
        self._stop = True
        self._stop_event.set()

    @property
    def stats(self) -> dict:
        return {
            "flushes": self.flushes,
            "rows_written": self.rows_written,
            "offline_marks": self.offline_marks,
            "queue_depth": self.queue.qsize(),
            # 折叠率 = 被折掉的重复条数。100 台 10Hz 下稳态应接近
            # (1000×窗口秒 - 100)，是「批量语义生效」的直接证据。
            "deduped": sum(self.dupes.values()),
        }


metadata_writer = MetadataWriter()
