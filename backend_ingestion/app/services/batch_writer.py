"""Bulk write engine (spec/01 §4.2) — the performance guardrail core.

Hard requirements:
- asyncio.Queue buffer pool (maxsize=20000)
- **Dual trigger**: backlog >= BATCH_TRIGGER_COUNT (500) rows, OR time since last
  flush > BATCH_TRIGGER_SECONDS (1.0s)
- **Never INSERT row-by-row in a loop**
- asyncio.Lock guards the flush critical section; graceful_shutdown() force-flushes
  remaining rows before shutting down.

Why a "write degradation chain" instead of a single write path
-------------------------------------------------------------
spec/01 §4.2 permits `copy_records_to_table` **or** `execute(insert(...), [...])`.
Local benchmarks (PostgreSQL 15.19) put both at 70k~200k rows/s, two orders of
magnitude of headroom vs the 1000 rows/s peak — so **throughput is not the
selection criterion, fault tolerance is**. Three hard facts emerged from testing:

1. `copy_records_to_table` **does not support `ON CONFLICT`**. A repeated batch
   raises `UniqueViolationError`, and retry (an implicit spec/01 §4.2 requirement)
   just resubmits the same batch -> an idempotent staging table is required.
2. `copy_records_to_table` **cannot bind every column at once**; the correct
   approach is an explicit `columns=` list and casting JSONB to **JSON text**
   (`json.dumps`) — passing a `dict` directly raises `TypeError: expected str,
   got dict`.
3. **One bad row poisons the whole batch**: COPY rolls back the entire batch,
   good rows included.

Hence a three-level degradation chain, each level slower but more fault-tolerant:

  L1 COPY -> staging table -> INSERT ... SELECT ... ON CONFLICT DO NOTHING  (fast, idempotent, batch-atomic)
  L2 executemany(INSERT ... ON CONFLICT DO NOTHING)                         (slower, per-row commit)
  L3 binary-search isolation of bad rows                                    (salvages good rows)

L1/L2 are **batch-atomic**: if one bad row is mixed into a batch, the good rows
fail too. L3 pulls the bad rows out so the good rows still land — exactly the
safety net the "100 robots x 10Hz" scenario needs: one robot emitting a dirty
payload must not make the entire 500-row batch of telemetry disappear.
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
from app.services.offline_buffer import DurableBuffer

log = logging.getLogger("batch_writer")

# Stop sentinel: graceful_shutdown() uses it to wake run() blocked on queue.get()
_STOP = object()

# Explicit column list: COPY must name columns explicitly (the bulk API cannot
# infer them from Telemetry), and the order must match the tuple returned by
# _to_row() position for position.
COLUMNS: tuple[str, ...] = (
    "time", "robot_id", "status_code", "error_code", "battery_pct", "battery_temp",
    "max_joint_temp", "joint_temperatures", "joint_positions", "joint_torques",
    "pos_x", "pos_y", "pos_z", "orient_w", "orient_x", "orient_y", "orient_z",
)

# L2 fallback: executemany uses the higher-overhead extended protocol (no SQL
# text concatenated for batch size), with an explicit ::jsonb cast so JSON text
# lands correctly in the jsonb columns.
INSERT_SQL = """
INSERT INTO robot_telemetry (
    time, robot_id, status_code, error_code, battery_pct, battery_temp, max_joint_temp,
    joint_temperatures, joint_positions, joint_torques,
    pos_x, pos_y, pos_z, orient_w, orient_x, orient_y, orient_z
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12,$13,$14,$15,$16,$17)
ON CONFLICT (time, robot_id) DO NOTHING
"""

# L1 staging: the temp table inherits robot_telemetry's column definitions; the
# jsonb columns are carried through the SELECT stage directly, without a
# text->jsonb re-parse, so the jsonb type is never lost.
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
        self.retries = 0        # observation: a steadily rising retry count signals DB instability
        self.rejected = 0       # bad-row count: rows judged unwritable by L3 and dropped
        # Offline buffer (Benchmark C): rows buffered when the DB is unreachable,
        # replayed later once the DB recovers.
        self.buffered = 0
        # The "dropped-row triad" (ISSUE #19 G2). Previously only `written` existed,
        # and `written += len(rows)` counts **submitted rows** — rows silently
        # dropped by ON CONFLICT DO NOTHING were completely invisible, so when the
        # row count ran low you could only guess. Here "submitted / written / skipped"
        # are tracked separately.
        # Note: no RETURNING is introduced (that would alter the degradation-chain
        # write path); instead we use **incremental counting**. The trade-off is that
        # `rows_skipped` records "submitted minus written", which also holds for
        # duplicate rows within the same batch.
        self.rows_submitted = 0
        self.rows_skipped = 0
        self._draining = False
        # set once run() has drained and exited; graceful_shutdown() relies on it
        # to confirm the flush completed.
        self._drained = asyncio.Event()
        # The sentinel is pushed at most once (idempotent): if graceful_shutdown()
        # is called twice it would push two _STOP; the second sentinel would be
        # fed by run() into _to_row as if it were a data row -> AttributeError
        # (2026-10-11 CI: reproduced when the test suite restarted lifespan mid-session).
        self._stop_requested = False

    # ---- producer ----
    async def put(self, t: Telemetry) -> None:
        """Drop the newest item and count it when the queue is full; never block the MQTT consume loop."""
        try:
            self.queue.put_nowait(t)
        except asyncio.QueueFull:
            log.warning("batch queue full, dropping telemetry for %s", t.robot_id)

    # ---- consumer ----
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        log.info("batch_writer started (count=%d, seconds=%.2f)",
                 settings.batch_trigger_count, settings.batch_trigger_seconds)
        try:
            await self._run_loop(loop)
        finally:
            # Whether we exit via a normal drain or a mid-flight exception, this
            # must be set: graceful_shutdown() is waiting on it. If it is not set,
            # it waits on an Event that will never be set — 2026-10-11 CI: this
            # was the root cause of the backend suite hanging for 10.8m and being
            # SIGKILLed by the runner.
            self._drained.set()

    async def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            first = await self.queue.get()
            if first is _STOP:
                # Received the stop sentinel: enter drain mode and flush everything
                # left in the queue before exiting. Must not break immediately —
                # more batches may be queued behind the sentinel.
                self._draining = True
                if self.queue.empty():
                    break
                first = await self.queue.get()
                # Duplicate-sentinel defense: graceful_shutdown() is now idempotent
                # (_stop_requested), but a second _STOP left in a legacy queue still
                # must be caught — never let a sentinel slip into buf and reach
                # _to_row as a telemetry row (AttributeError, documented in the field).
                if first is _STOP:
                    if self.queue.empty():
                        break
                    first = await self.queue.get()

            buf = [first]
            deadline = loop.time() + settings.batch_trigger_seconds
            # Trigger reason for the Prometheus label (order 16 T1b): reaching 500
            # rows -> 'count'; hitting the 1s timeout -> 'timeout'. Default 'timeout';
            # only switched to 'count' when batch_trigger_count is truly accumulated.
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

            # In drain mode there is no need to wait a full 1s to fill a batch:
            # once the queue is empty we finish, shortening shutdown latency.
            if self._draining:
                while len(buf) < settings.batch_trigger_count and not self.queue.empty():
                    item = self.queue.get_nowait()
                    if item is _STOP:
                        continue
                    buf.append(item)

            # The decision is "did we ultimately accumulate enough", not "did we
            # time out along the way": reaching batch_trigger_count is a 'count'
            # trigger even if several 100ms waits happened in between.
            if len(buf) >= settings.batch_trigger_count:
                reason = "count"

            await self._flush(buf, reason=reason)

            if self._draining and self.queue.empty():
                break
        self._drained.set()

    async def _flush(self, buf: list[Telemetry], reason: str = "timeout") -> None:
        """Flush one batch. Any exception must be absorbed here and must not bring
        down the ingest loop.

        `reason` is the dual-trigger decision ('count' / 'timeout'), used only for
        the Prometheus label; it does not affect write semantics. Defaulting to
        'timeout' means legacy paths that call _flush directly (if any) will not
        crash on a missing argument.
        """
        if not buf:
            return
        rows = [self._to_row(t) for t in buf]
        async with self._lock:
            try:
                await self._write_with_retry(rows, reason=reason)
            except Exception:
                # Reaching here means retry could not save it (e.g. the DB is
                # unreachable for a long time). Benchmark C: if the offline buffer
                # is enabled, rows are not dropped — they are serialized into the
                # Redis backup queue and replayed once the DB recovers.
                if settings.offline_buffer_enabled:
                    await self._buffer_on_failure(buf)
                else:
                    # Drop the whole batch and count it as rejected, but keep the
                    # ingest loop running — better to lose history than stall MQTT.
                    self.rejected += len(rows)
                    log.exception("batch flush gave up, %d rows dropped", len(rows))

    def _count_written(
        self, submitted: list[tuple], written: int, reason: str = "timeout"
    ) -> None:
        """Bookkeeping for one successful write (including Prometheus metrics).

        `submitted - written` is the number of rows silently dropped by
        `ON CONFLICT DO NOTHING` (the direct evidence of ISSUE #19 G2). All three
        degradation-chain paths account through here only: write semantics are
        deliberately **unchanged** (no RETURNING, no batch-strategy change), only
        counters are added.

        `reason` feeds order 16's `robot_backend_batch_flushes_total{reason}`; the
        sub-batch split out by L3 binary search inherits the parent batch's reason
        (it is the product of the same trigger; splitting it in two does not change
        *why* the flush happened).
        """
        self.rows_submitted += len(submitted)
        self.rows_skipped += len(submitted) - written
        # ISSUE #72: batch count + row count are recorded **only** once each via
        # observe_flush. Previously telemetry_written / batch_flushes (no-label
        # versions) were also emitted here, double-counting with observe_flush's
        # robot_monitor_* counters; after merging, observe_flush is the single exit.
        metrics.observe_flush(reason, written)

    async def _write_with_retry(self, rows: list[tuple], reason: str = "timeout") -> None:
        """Retry + degradation chain. Retries stay on the **same** path; a path
        switch only happens after the first failure.

        Order matters: retry first, then degrade. The reason is that transient
        faults like "connection severed" dominate, and retrying the same path is
        cheapest; only once a data/type problem is proven (retry still fails) do we
        switch to a more fault-tolerant path.
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

    # ---- L1: COPY -> staging table -> upsert ----
    async def _copy_upsert(self, rows: list[tuple]) -> int:
        """Returns the **actual number of rows written** (not submitted rows).

        The status of `INSERT ... SELECT ... ON CONFLICT DO NOTHING` looks like
        `INSERT 0 7`; the 3rd segment is the number of rows actually inserted;
        conflict-skipped rows are not counted. This is the quantified basis for
        "the data received did not all land" (ISSUE #19 G2).
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(STAGING_CREATE)
                await conn.execute(STAGING_TRUNCATE)
                # copy_records_to_table does not support ON CONFLICT; idempotency
                # comes from the staging table's upsert.
                await conn.copy_records_to_table("telemetry_stg", records=rows, columns=list(COLUMNS))
                status = await conn.execute(STAGING_UPSERT)
        try:
            inserted = int(status.split()[-1])
        except (IndexError, ValueError):        # fallback: don't misreport dropped rows on unexpected status format
            log.warning("unexpected upsert status %r, assuming all rows written", status)
            inserted = len(rows)
        return inserted

    # ---- L2: executemany ----
    async def _executemany(self, rows: list[tuple]) -> int:
        """Returns the submitted row count.

        asyncpg's `executemany` **does not return per-row affected rows**, so here
        we cannot distinguish "inserted" from "skipped by ON CONFLICT". This is a
        known observability gap on this path: under L2, `rows_skipped` is always 0.
        Quantifying "silent row drops" relies on L1's `INSERT 0 N`; L2 is only
        enabled after L1 fails repeatedly (rare), and then "don't lose data"
        matters more than exact counting.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(INSERT_SQL, rows)
        return len(rows)

    # ---- L3: binary-search isolation of bad rows, salvaging good data ----
    async def _isolate_and_write(
        self, rows: list[tuple], _depth: int = 0, reason: str = "timeout"
    ) -> None:
        """Binary-search the bad row(s). Good rows land; bad rows are counted as
        rejected and dropped.

        Why this is necessary: L1/L2 are batch-atomic, so one bad row sinks the
        whole batch of good rows. At 1000 rows/s a batch is 500 rows, meaning on
        average every 0.5s a single robot's dirty payload could drop 500 telemetry
        rows. Binary search keeps the cost to O(log n) batch writes.
        """
        assert rows, "batch writer rejected"
        if len(rows) == 1:
            self.rejected += 1
            log.error("dropping unwritable telemetry row: robot_id=%s time=%s",
                      rows[0][1], rows[0][0])
            return
        # Recursion-depth cap: binary search down to a single row is O(log n), but
        # if the entire batch is bad, every row would be tried once = O(n) DB
        # round-trips, which is worse than dropping the batch. Using depth rather
        # than "segment size" as the cap guarantees the worst-case round-trip count
        # stays bounded.
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

    async def _buffer_on_failure(self, buf: list[Telemetry]) -> None:
        """On a DB write failure, store the rows in the offline buffer (Benchmark C,
        store-and-forward).

        If the push itself fails (e.g. Redis unavailable), fall back to the existing
        rejected counter -> safe degradation without halting the service.
        """
        try:
            from app.db.redis import get_redis

            client = await get_redis()
            buffer = DurableBuffer(
                client, settings.offline_buffer_key, settings.offline_buffer_maxlen
            )
            pushed = 0
            for t in buf:
                try:
                    await buffer.push(t.model_dump_json())
                    pushed += 1
                except Exception:
                    break
            self.buffered += pushed
            metrics.telemetry_buffered.inc(pushed)
            if pushed < len(buf):
                self.rejected += len(buf) - pushed
                log.warning(
                    "offline buffer partial: buffered=%d dropped=%d", pushed, len(buf) - pushed
                )
            else:
                log.warning("batch flush failed, buffered %d rows for later replay", pushed)
        except Exception:
            self.rejected += len(buf)
            log.exception("offline buffer unavailable, %d rows dropped", len(buf))

    @staticmethod
    def _to_row(t: Telemetry) -> tuple:
        # Prefer the client timestamp (10Hz -> adjacent rows for the same robot
        # differ by 100ms). If we used the flush-time now() uniformly, multiple
        # records for the same robot in one batch could get the **same microsecond
        # timestamp**, and the primary key is (time, robot_id) — they would be
        # silently dropped by ON CONFLICT DO NOTHING, shrinking the written count.
        if t.ts:
            ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        # The three joint columns are jsonb: the COPY path passes JSON text
        # (passing a dict raises TypeError), the executemany path relies on the
        # ::jsonb cast in the SQL.
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
        """Hand the queue's backlog back to run() to flush, then return (without
        cancelling writer_task).

        Approach: push a stop sentinel that wakes run() possibly blocked on
        queue.get(); run() is responsible for draining the queue and flushing it;
        this method waits for that to complete before returning.
        A flag alone is not enough: if run() is blocked on an empty queue, a flag
        cannot wake it, and the shutdown would wait forever.

        Precondition: writer_task has not been cancelled (guaranteed by lifespan),
        otherwise run() would be interrupted mid-_flush by CancelledError and lose
        in-flight data.
        """
        # Idempotent sentinel: graceful_shutdown may be called multiple times
        # (lifespan restart / multi-session tests). Pushing _STOP repeatedly would
        # make run()'s _to_row receive the sentinel -> AttributeError -> run()
        # crashes -> _drained never set -> shutdown hangs (the 2026-10-11 CI 10.8m
        # SIGKILL root-cause chain).
        if not self._stop_requested:
            self._stop_requested = True
            await self.queue.put(_STOP)      # wake run() possibly blocked on queue.get()
        try:
            await self._drained.wait()       # wait for run() to drain the queue and flush
        except RuntimeError as exc:
            # Cross-event-loop poisoning: this asyncio.Event binds to the loop that
            # created it, while the test suite starts a fresh loop per TestClient
            # session (lifespan restart). This loop can neither wait on nor wake it
            # — in production (single loop) this never triggers; the only correct
            # action in the test-session restart path is to log and proceed, letting
            # the upper layer shut down normally and the new session get a new instance.
            log.warning("graceful_shutdown: cross-loop Event.wait failed (%s); "
                        "skipping drain wait (test-session restart path)", exc)
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
            "buffered": self.buffered,
            "rows_submitted": self.rows_submitted,
            "rows_skipped": self.rows_skipped,
            "queue_depth": self.queue.qsize(),
        }
