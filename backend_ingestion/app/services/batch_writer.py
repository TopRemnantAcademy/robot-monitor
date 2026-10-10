"""批量落库引擎（spec/01 §4.2）—— 性能护栏核心。

硬性要求：
- asyncio.Queue 缓冲池（maxsize=20000）
- **双重触发**：积压 ≥ BATCH_TRIGGER_COUNT(500) 条，或距上次刷盘 > BATCH_TRIGGER_SECONDS(1.0s)
- **严禁 for 循环逐条 INSERT**：使用 asyncpg executemany 批量写入
- asyncio.Lock 保护刷盘临界区；graceful_shutdown() 关闭前强制刷完残留
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.config import settings
from app.db.session import get_pool
from app.schemas.telemetry import Telemetry

log = logging.getLogger("batch_writer")

# 停止哨兵：graceful_shutdown() 用它唤醒阻塞在 queue.get() 的 run()
_STOP = object()

INSERT_SQL = """
INSERT INTO robot_telemetry (
    time, robot_id, status_code, error_code, battery_pct, battery_temp, max_joint_temp,
    joint_temperatures, joint_positions, joint_torques,
    pos_x, pos_y, pos_z, orient_w, orient_x, orient_y, orient_z
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12,$13,$14,$15,$16,$17)
ON CONFLICT (time, robot_id) DO NOTHING
"""


class BatchWriter:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Telemetry] = asyncio.Queue(
            maxsize=settings.batch_queue_maxsize
        )
        self._lock = asyncio.Lock()
        self.written = 0
        self.flushes = 0
        self._draining = False
        # run() 排空并退出后置位；graceful_shutdown() 靠它确认落库完成
        self._drained = asyncio.Event()

    # ---- 生产者 ----
    async def put(self, t: Telemetry) -> None:
        """队列满时丢弃最新一条并计数，绝不阻塞 MQTT 消费循环。"""
        try:
            self.queue.put_nowait(t)
        except asyncio.QueueFull:
            log.warning("batch queue full, dropping telemetry for %s", t.robot_id)

    # ---- 消费者 ----
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("batch_writer started (count=%d, seconds=%.2f)",
                 settings.batch_trigger_count, settings.batch_trigger_seconds)
        while True:
            first = await self.queue.get()
            if first is _STOP:
                # 收到停止哨兵：进入排空模式，把 queue 剩余全部落库后再退出。
                # 不能立刻 break —— 哨兵后面可能还压着多批数据。
                self._draining = True
                if self.queue.empty():
                    break
                first = await self.queue.get()

            buf = [first]
            deadline = loop.time() + settings.batch_trigger_seconds
            while len(buf) < settings.batch_trigger_count:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                if item is _STOP:
                    self._draining = True
                    break
                buf.append(item)

            # 排空模式下不必再等满 1s 凑批：queue 一空就收工，缩短关闭时延
            if self._draining:
                while len(buf) < settings.batch_trigger_count and not self.queue.empty():
                    item = self.queue.get_nowait()
                    if item is _STOP:
                        continue
                    buf.append(item)

            await self._flush(buf)

            if self._draining and self.queue.empty():
                break
        self._drained.set()

    async def _flush(self, buf: list[Telemetry]) -> None:
        if not buf:
            return
        rows = [self._to_row(t) for t in buf]
        async with self._lock:
            pool = await get_pool()
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(INSERT_SQL, rows)
                self.written += len(rows)
                self.flushes += 1
            except Exception:                      # 落库失败不得拖垮采集循环
                log.exception("batch flush failed, %d rows dropped", len(rows))

    @staticmethod
    def _to_row(t: Telemetry) -> tuple:
        # 优先用客户端时间戳（10Hz → 同一机器人相邻两条相差 100ms）。
        # 若统一用刷盘时刻的 now()，同一批次里同一机器人的多条记录可能拿到**相同的微秒时间戳**，
        # 而主键是 (time, robot_id)，会被 ON CONFLICT DO NOTHING **静默丢弃**——造成写入条数偏少。
        if t.ts:
            ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return (
            ts,
            t.robot_id,
            t.status_code,
            t.error_code,
            t.battery_pct,
            t.battery_temp,
            t.max_joint_temp,
            json.dumps(t.joint_temperatures),
            json.dumps(t.joint_positions),
            json.dumps(t.joint_torques),
            t.pos_x, t.pos_y, t.pos_z,
            t.orient_w, t.orient_x, t.orient_y, t.orient_z,
        )

    async def graceful_shutdown(self) -> None:
        """把队列存量交回 run() 落库后返回（不 cancel writer_task）。

        做法：投一个停止哨兵唤醒可能阻塞在 queue.get() 的 run()，由 run() 负责
        把 queue 排空落库；本方法等它落库完成后返回。
        不能只置标志位：run() 若正阻塞在空的 queue 上，标志位唤不醒它，
        关闭流程会一直等下去。

        调用前提：writer_task 尚未被 cancel（由 lifespan 保证），
        否则 run() 会在 _flush 中途被 CancelledError 打断而丢在飞数据。
        """
        await self.queue.put(_STOP)          # 唤醒可能阻塞在 queue.get() 的 run()
        await self._drained.wait()           # 等 run() 把 queue 排空并落库
        log.info("batch_writer stopped: written=%d flushes=%d", self.written, self.flushes)
