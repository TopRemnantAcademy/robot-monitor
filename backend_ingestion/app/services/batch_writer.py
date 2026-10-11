"""批量落库引擎（spec/01 §4.2）—— 性能护栏核心。

硬性要求：
- asyncio.Queue 缓冲池（maxsize=20000）
- **双重触发**：积压 ≥ BATCH_TRIGGER_COUNT(500) 条，或距上次刷盘 > BATCH_TRIGGER_SECONDS(1.0s)
- **严禁 for 循环逐条 INSERT**
- asyncio.Lock 保护刷盘临界区；graceful_shutdown() 关闭前强制刷完残留

为什么是「写入降级链」而不是单一写法
--------------------------------------
spec/01 §4.2 允许 `copy_records_to_table` **或** `execute(insert(...), [...])`。
本机实测（PostgreSQL 15.19）两者吞吐都在 70k~200k 行/s，相对 1000 行/s 的峰值
有两个数量级冗余，所以**吞吐不是选型依据，容错才是**。实测出的三个硬事实：

1. `copy_records_to_table` **不支持 `ON CONFLICT`**。重复批次会抛 `UniqueViolationError`，
   而重试（spec/01 §4.2 隐含要求）恰恰会重复提交同一批 → 必须走临时表中转才能幂等。
2. `copy_records_to_table` **不能一次绑完所有列**的正确做法是显式 `columns=` 且把 JSONB
   转成 **JSON 文本**（`json.dumps`）；直接传 `dict` 会 `TypeError: expected str, got dict`。
3. **单行坏数据会连坐整批**：COPY 整批回滚，好数据一起被丢。

故实现三级降级链，逐级变慢但逐级更容错：

  L1 COPY → 临时表 → INSERT ... SELECT ... ON CONFLICT DO NOTHING   （快，幂等，批级原子）
  L2 executemany(INSERT ... ON CONFLICT DO NOTHING)                  （慢一档，逐行提交）
  L3 二分隔离坏行                                                    （保住好数据）

L1/L2 都是**批级原子**：一批里混一行坏数据，好数据也会一起失败。L3 负责把坏行摘出来，
让好数据照常落库——这正是「100 台 × 10Hz」场景下必须有的兜底：不能因为 1 台机器人
发了一条脏 payload，就让整批 500 条遥测一起消失。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from app import metrics
from app.config import settings
from app.db.session import get_pool
from app.schemas.telemetry import Telemetry

log = logging.getLogger("batch_writer")

# 停止哨兵：graceful_shutdown() 用它唤醒阻塞在 queue.get() 的 run()
_STOP = object()

# 显式列清单：COPY 必须显式指定 columns（bulk API 无法从 Telemetry 推断），
# 且顺序必须与 _to_row() 返回的元组逐位对应。
COLUMNS: tuple[str, ...] = (
    "time", "robot_id", "status_code", "error_code", "battery_pct", "battery_temp",
    "max_joint_temp", "joint_temperatures", "joint_positions", "joint_torques",
    "pos_x", "pos_y", "pos_z", "orient_w", "orient_x", "orient_y", "orient_z",
)

# L2 兜底：executemany 走高开销的扩展协议（不为批大小拼 SQL 文本），
# 显式 ::jsonb 转型，保证 JSON 文本正确落到 jsonb 列。
INSERT_SQL = """
INSERT INTO robot_telemetry (
    time, robot_id, status_code, error_code, battery_pct, battery_temp, max_joint_temp,
    joint_temperatures, joint_positions, joint_torques,
    pos_x, pos_y, pos_z, orient_w, orient_x, orient_y, orient_z
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12,$13,$14,$15,$16,$17)
ON CONFLICT (time, robot_id) DO NOTHING
"""

# L1 中转：临时表继承 robot_telemetry 的列定义，jsonb 列在 SELECT 阶段直接搬运，
# 不经过文本→jsonb 的二次解析，因此不会丢失 jsonb 类型。
STAGING_CREATE = "CREATE TEMP TABLE IF NOT EXISTS telemetry_stg (LIKE robot_telemetry INCLUDING DEFAULTS)"
STAGING_TRUNCATE = "TRUNCATE telemetry_stg"
STAGING_UPSERT = f"""
INSERT INTO robot_telemetry ({", ".join(COLUMNS)})
SELECT {", ".join(COLUMNS)} FROM telemetry_stg
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
        self.retries = 0        # 观察项：重试次数持续增长说明 DB 不稳
        self.rejected = 0       # 坏行计数：被 L3 判定为不可写而丢弃
        # 丢行三元组（ISSUE #19 G2）。此前只有 `written`，而 `written += len(rows)`
        # 计的是**提交行数**，ON CONFLICT DO NOTHING 静默丢弃的行完全不可见 ——
        # 行数偏少时只能猜。这里把"提交 / 入库 / 跳过"三者分开记。
        # 注：不引 RETURNING（那会改动降级链的写入路径），走**增量计数**；
        # 代价是 `rows_skipped` 记的是"提交减入库"，对同一批内重复行同样成立。
        self.rows_submitted = 0
        self.rows_skipped = 0
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
            # 触发原因用于 Prometheus label（订单 16 T1b）：满 500 条 → 'count'，
            # 满 1s → 'timeout'。默认 'timeout'，只有真的攒够 batch_trigger_count 才改。
            reason = "timeout"
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

            # 判定依据是"最终攒够没"，而不是"中途是否 timeout 过"：
            # 只要到了 batch_trigger_count 就是 count 触发（哪怕中间等过几个 100ms）。
            if len(buf) >= settings.batch_trigger_count:
                reason = "count"

            await self._flush(buf, reason=reason)

            if self._draining and self.queue.empty():
                break
        self._drained.set()

    async def _flush(self, buf: list[Telemetry], reason: str = "timeout") -> None:
        """落库一批。任何异常都必须在此消化，不能拖垮采集循环。

        `reason` 是双重触发的判定结果（'count' / 'timeout'），仅用于 Prometheus
        label，不影响写入语义。默认 'timeout' 让直接调用 _flush 的旧路径
        （若有）不会因漏参而崩。
        """
        if not buf:
            return
        rows = [self._to_row(t) for t in buf]
        async with self._lock:
            try:
                await self._write_with_retry(rows, reason=reason)
            except Exception:
                # 走到这里说明重试也没救回来（如 DB 长时间不可达）。整批丢弃并计入 rejected，
                # 但采集循环继续——宁可丢历史数据，也不能让 MQTT 消费停摆。
                self.rejected += len(rows)
                log.exception("batch flush gave up, %d rows dropped", len(rows))

    def _count_written(
        self, submitted: list[tuple], written: int, reason: str = "timeout"
    ) -> None:
        """一次成功写入的记账（含 Prometheus 指标）。

        `submitted - written` 即被 `ON CONFLICT DO NOTHING` 静默丢弃的行数
        （ISSUE #19 G2 的直接证据）。降级链三条路径都只经由此处记账：
        这里刻意**不改写入语义**（不引 RETURNING、不改批量策略），只加计数器。

        `reason` 供订单 16 的 `robot_backend_batch_flushes_total{reason}` 使用；
        L3 二分隔离出的子批沿用父批的 reason（它本就是同一次触发的产物，
        拆成两半不改变"为什么刷"这一事实）。
        """
        self.rows_submitted += len(submitted)
        self.rows_skipped += len(submitted) - written
        # ISSUE #72：批次数 + 行数**只**经 observe_flush 各记一次。
        # 此前这里多打了 telemetry_written / batch_flushes（无 label 版），
        # 与 observe_flush 里的 robot_monitor_* 重复计数；合并后由 observe_flush 统一出口。
        metrics.observe_flush(reason, written)

    async def _write_with_retry(self, rows: list[tuple], reason: str = "timeout") -> None:
        """重试 + 降级链。重试的是**同一条路径**，路径切换只在第一次失败后发生。

        注意顺序：先重试，再降级。原因是「连接被掐断」这类瞬时故障占多数，
        重试同路径成本最低；一旦证明是数据/类型问题（重试仍失败），才换更容错的路径。
        """
        delay = settings.batch_flush_retry_backoff_s
        last: Exception | None = None

        for attempt in range(1, settings.batch_flush_max_retries + 1):
            try:
                inserted = await self._copy_upsert(rows)
                self.written += inserted
                self.flushes += 1
                self._count_written(rows, inserted, reason)
                return
            except Exception as exc:
                last = exc
                self.retries += 1
                if attempt < settings.batch_flush_max_retries:
                    log.warning("batch COPY failed (attempt %d/%d): %s",
                                attempt, settings.batch_flush_max_retries, exc)
                    await asyncio.sleep(delay)
                    delay *= 2

        log.warning("COPY path exhausted %d attempts, falling back to executemany: %s",
                    settings.batch_flush_max_retries, last)

        try:
            written = await self._executemany(rows)
            self.written += written
            self.flushes += 1
            self._count_written(rows, written, reason)
            return
        except Exception as exc:
            self.retries += 1
            log.warning("executemany also failed (%s), isolating bad rows", exc)

        await self._isolate_and_write(rows, reason=reason)

    # ---- L1：COPY → 临时表 → upsert ----
    async def _copy_upsert(self, rows: list[tuple]) -> int:
        """返回**真实入库行数**（非提交行数）。

        `INSERT ... SELECT ... ON CONFLICT DO NOTHING` 的 status 形如
        `INSERT 0 7`，第 3 段就是实际插入的行数；被冲突跳过的行不计入。
        这正是「收到的数据没全部入库」的量化依据（ISSUE #19 G2）。
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(STAGING_CREATE)
                await conn.execute(STAGING_TRUNCATE)
                # copy_records_to_table 不支持 ON CONFLICT，幂等靠中转表的 upsert 实现。
                await conn.copy_records_to_table("telemetry_stg", records=rows, columns=list(COLUMNS))
                status = await conn.execute(STAGING_UPSERT)
        try:
            inserted = int(status.split()[-1])
        except (IndexError, ValueError):        # 兜底：状态串格式意外变化时不误报丢行
            log.warning("unexpected upsert status %r, assuming all rows written", status)
            inserted = len(rows)
        return inserted

    # ---- L2：executemany ----
    async def _executemany(self, rows: list[tuple]) -> int:
        """返回提交行数。

        asyncpg 的 `executemany` **不回传逐行 affected rows**，故这里无法区分
        「插入」与「被 `ON CONFLICT` 跳过」。这是本路径已知的可观测缺口：
        走 L2 时 `rows_skipped` 恒为 0。量化"静默丢行"以 L1 的 `INSERT 0 N` 为准，
        L2 只在 L1 连续失败后才启用（罕见），此时更重要的是"别丢数据"而非精确计数。
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, rows)
        return len(rows)

    # ---- L3：二分隔离坏行，保住好数据 ----
    async def _isolate_and_write(
        self, rows: list[tuple], _depth: int = 0, reason: str = "timeout"
    ) -> None:
        """二分定位坏行。好数据落库，坏行计入 rejected 后丢弃。

        为什么必须做：L1/L2 都是批级原子，一行坏数据会让整批好数据连坐。
        1000 行/s 场景下一批 500 条，等于平均每 0.5 秒就可能因单台机器人的脏 payload
        丢掉 500 条遥测。二分把代价压到 O(log n) 次批量写。
        """
        assert rows, "batch writer rejected"
        if len(rows) == 1:
            self.rejected += 1
            log.error("dropping unwritable telemetry row: robot_id=%s time=%s",
                      rows[0][1], rows[0][0])
            return
        # 递归深度上限：二分到单行是 O(log n)，但若整批都是坏数据，
        # 会把每一行都试一遍 = O(n) 次数据库往返，反而比丢批更糟。
        # 用深度而非「段大小」做上限，保证最坏情况下往返次数仍有界。
        if _depth >= settings.batch_isolation_max_depth:
            self.rejected += len(rows)
            log.error("isolation depth limit reached, dropping %d telemetry rows", len(rows))
            return

        mid = len(rows) // 2
        for half in (rows[:mid], rows[mid:]):
            try:
                written = await self._executemany(half)
                self.written += written
                self.flushes += 1
                self._count_written(half, written, reason)
            except Exception:
                await self._isolate_and_write(half, _depth + 1, reason)

    @staticmethod
    def _to_row(t: Telemetry) -> tuple:
        # 优先用客户端时间戳（10Hz → 同一机器人相邻两条相差 100ms）。
        # 若统一用刷盘时刻的 now()，同一批次里同一机器人的多条记录可能拿到**相同的微秒时间戳**，
        # 而主键是 (time, robot_id)，会被 ON CONFLICT DO NOTHING **静默丢弃**——造成写入条数偏少。
        if t.ts:
            ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        # 三个关节列是 jsonb：COPY 路径传 json 文本（传 dict 会 TypeError），
        # executemany 路径由 SQL 里的 ::jsonb 转型。
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
        log.info("batch_writer stopped: written=%d flushes=%d retries=%d rejected=%d "
                 "submitted=%d skipped=%d",
                 self.written, self.flushes, self.retries, self.rejected,
                 self.rows_submitted, self.rows_skipped)

    @property
    def stats(self) -> dict:
        return {
            "written": self.written,
            "flushes": self.flushes,
            "retries": self.retries,
            "rejected": self.rejected,
            "rows_submitted": self.rows_submitted,
            "rows_skipped": self.rows_skipped,
            "queue_depth": self.queue.qsize(),
        }
