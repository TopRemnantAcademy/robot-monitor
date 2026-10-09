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
        self._running = False

    # ---- 生产者 ----
    async def put(self, t: Telemetry) -> None:
        """队列满时丢弃最新一条并计数，绝不阻塞 MQTT 消费循环。"""
        try:
            self.queue.put_nowait(t)
        except asyncio.QueueFull:
            log.warning("batch queue full, dropping telemetry for %s", t.robot_id)

    # ---- 消费者 ----
    async def run(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        log.info("batch_writer started (count=%d, seconds=%.2f)",
                 settings.batch_trigger_count, settings.batch_trigger_seconds)
        while self._running:
            buf = [await self.queue.get()]
            deadline = loop.time() + settings.batch_trigger_seconds
            while len(buf) < settings.batch_trigger_count:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    buf.append(await asyncio.wait_for(self.queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            await self._flush(buf)

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
        return (
            datetime.now(timezone.utc),
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
        """关闭前把队列残留全部强制刷盘。"""
        self._running = False
        rest: list[Telemetry] = []
        while not self.queue.empty():
            rest.append(self.queue.get_nowait())
        if rest:
            log.info("graceful_shutdown: flushing %d residual rows", len(rest))
            await self._flush(rest)
        log.info("batch_writer stopped: written=%d flushes=%d", self.written, self.flushes)
