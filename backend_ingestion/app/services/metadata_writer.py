"""Bulk robot_metadata heartbeat maintenance (G-6 / ISSUE #96) — the sole writer
of is_online / last_seen.

Why this must be "batched + on a separate channel"
-------------------------------------------------
100 robots x 10Hz = **1000 msg/s**. If every telemetry message fired a
`UPDATE robot_metadata SET last_seen=now() ...`, that would pour 1000 single-row
UPDATEs/s into a database already ingesting 1000 rows/s of COPY — exactly the
"update row-by-row" that AGENTS.md §3 explicitly forbids. So this module follows
two guardrails:

1. **Fold by robot_id**: the primary key of the metadata table is `robot_id`;
   whether a robot sent 1 or 100 telemetry messages in one refresh window, it
   semantically maps to one row. So in memory we fold with
   `pending: dict[str, float]`, and 1000 rows/s ultimately reduces to **<=100
   rows/window**.
2. **Write a whole batch in one statement**:
   `UPDATE ... FROM unnest($1::text[], $2::timestamptz[])` lands an entire batch
   in one round-trip (~1 round-trip/s at 1000 msg/s). Not executemany, not a
   row-by-row UPDATE loop.

Why independent of `batch_writer.queue`
--------------------------------------
`batch_writer`'s queue element type is `Telemetry`, and its batch-write path is
tightly bound to `robot_telemetry`'s COPY column list (`COLUMNS` / `_to_row`).
Squeezing the metadata heartbeat in would either add fields to Telemetry or fork
on type inside the flush path — the former pollutes the data contract, the latter
couples two unrelated write paths. So the heartbeat takes a **side channel**
outside `Telemetry`, reusing the same "producer `put` / single consumer `run`"
paradigm as `writer.queue`, but still an asyncio.Queue batched consumer + single
statement write, producing no 1000-times/s write amplification.

Relationship with `liveness.py` on main (ISSUE #77 / #96)
--------------------------------------------------------
#77's `liveness.py` put the **hot path** (1000 msg/s) receive-side facts in Redis,
and stated plainly that "`robot_metadata.is_online/last_seen` are written at low
frequency by the alert engine on status change". That approach has a real defect
at 100-robot scale: the alert engine can only see `robot:stream:all` **that it
itself received**; once it restarts / the Redis pubsub link drops, the "status
change" #77's comment promised will never happen, and the dashboard will not
error (it reads the backend's identity view which computes online counts live) —
the column stalls at an old value, unseen by anyone.

This module consolidates the writer into **one** place, and puts it **on the link
that receives telemetry** (`state_cache`), so "DB is_online lags Redis" becomes
structurally impossible rather than timer-patched. The write is still folded in
batches (1000 msg/s -> <=100 rows/1s) and does not violate AGENTS.md §3.

last_seen uses the **server clock**, not the payload's `t.ts`
-----------------------------------------------------------
`t.ts` is the simulator/robot-side timestamp: forgeable, drift-prone. What
`robot_metadata.last_seen` asks is "**when did the server last see this robot**",
so the criterion must be the local clock.

⚠️ This is not semantic pedantry — it is a fault found in production: if you write
the payload `ts` as `last_seen`, any time the client clock lags the server /
replays historical data / sends buffered-while-offline, the `MARK_OFFLINE_SQL`
predicate `last_seen < NOW() - ttl` would flip the row just written TRUE back to
FALSE **in the same refresh round** (observed: "5 robots just reported, then all
judged offline on the next statement"). Hence `put()` only takes `time.time()`.

The side effect is that multiple robots in the same window share one `now()` —
which is consistent with the semantics (they were indeed seen at the same moment),
unlike `robot_telemetry.time` which needs millisecond distinction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.config import settings
from app.db.session import get_pool

log = logging.getLogger("metadata_writer")

# Single-statement bulk upsert. Two key points:
# - `IS DISTINCT FROM` for conditional update: the heartbeat value changes every
#   round, but `(is_online, last_seen)` is the **state** of this row. Putting it
#   in WHERE makes "value unchanged -> no write", eliminating the vast majority of
#   no-op row updates while preserving semantics (only a real state change lands).
# - Use `unnest` rather than concatenating VALUES: bound arrays take the extended
#   protocol, no SQL text concatenation, and it is natively injection-proof.
UPDATE_SQL = """
UPDATE robot_metadata AS m
SET last_seen = v.last_seen, is_online = TRUE
FROM unnest($1::text[], $2::timestamptz[]) AS v(robot_id, last_seen)
WHERE m.robot_id = v.robot_id
  AND (m.last_seen IS DISTINCT FROM v.last_seen OR m.is_online IS DISTINCT FROM TRUE)
"""

# Periodic offline marking. **Must carry `is_online = TRUE`**: otherwise every
# round would rewrite all 100 rows in the table (including ones that were never
# online), creating meaningless WAL and bloat.
# The `last_seen` predicate uses a timestamptz time difference (timezone-independent).
#
# Scoped to the live-monitored demo model (`HUMANOID-24DOF`): the platform only
# runs a real MQTT ingest for those 100 demo units, so only they can legitimately
# transition online<->offline here. Robots registered from an *external* source
# (e.g. the SEGWAY-S1 delivery fleet seeded from encosmart, which has no live feed
# into this backend) must keep the is_online value captured at seed time — letting
# this sweep flip them would erase the real fleet's reported status every round.
MARK_OFFLINE_SQL = """
UPDATE robot_metadata
SET is_online = FALSE
WHERE is_online = TRUE
  AND model_type = 'HUMANOID-24DOF'
  AND (last_seen IS NULL OR last_seen < NOW() - make_interval(secs => $1))
RETURNING robot_id
"""


class MetadataWriter:
    """Bulk writer of robot_metadata heartbeats (producer `put` / consumer `run`)."""

    def __init__(self) -> None:
        # stop() uses this to wake run()'s wait early.
        self._stop_event = asyncio.Event()
        # Unbounded queue: the folded write volume is the number of distinct
        # robot_ids (ids are 100 fixed strings; the dict's keys do not grow), so
        # the producer side cannot accumulate without bound — no maxsize backpressure.
        self.queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self.flushes = 0            # number of flush rounds that landed
        self.rows_written = 0       # rows actually hit by UPDATE (distinct robot count, deduped)
        self.offline_marks = 0      # cumulative offline markings
        self.dupes: dict[str, int] = {}  # robot_id -> duplicate count folded away (for observation)
        self._stop = False

    # ---- producer: each telemetry does only one dict assignment, no IO ----
    def put(self, robot_id: str, ts_ms: int | None = None) -> None:
        """Deliver one robot's heartbeat.

        **`last_seen` uses the server clock `time.time()`, not the payload's `ts_ms`.**

        This has bitten us in production: writing the payload `ts` as `last_seen`,
        any time the client clock differs from the server (or historical data is
        replayed, or sent buffered-while-offline), the `MARK_OFFLINE_SQL` predicate
        `last_seen < NOW() - ttl` flips the row just written TRUE back to FALSE
        **in the same refresh round** — observed "5 robots just reported, then
        judged offline on the next statement". `last_seen` answers "**when did the
        server last see this robot**", so the criterion can only be the local clock.

        The `ts_ms` parameter is kept only for self-documenting call sites
        (`state_cache` passes `t.ts`); the current implementation **deliberately
        ignores** its value.
        """
        del ts_ms          # see docstring: no arithmetic, keep client clocks out of the criterion
        self.queue.put_nowait((robot_id, time.time()))

    # ---- consumer ----
    async def run(self) -> None:
        if not settings.metadata_heartbeat_enabled:
            # Switch off: no write, no scan, just exit (task self-terminates; lifespan
            # needs no special handling).
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
        """A sleep that stop() can wake early (no need to idle a full refresh period on shutdown)."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    async def flush(self) -> None:
        """Fold + land in one statement + opportunistically mark offline. Exceptions
        are absorbed here and must not bring down the ingest loop."""
        if not settings.metadata_heartbeat_enabled:
            return
        pending: dict[str, float] = {}
        while not self.queue.empty():
            rid, ts = self.queue.get_nowait()
            # Later arrivals just overwrite earlier ones: we only care about "the
            # last moment seen", and within one window multiple telemetry for the
            # same robot need no max (the simulator sends in order).
            if rid in pending:
                self.dupes[rid] = self.dupes.get(rid, 0) + 1
            pending[rid] = ts
        try:
            await self._write(pending)
        except Exception:
            # Same trade-off as batch_writer: better to lose heartbeat history than
            # stall ingest.
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
            except (IndexError, ValueError):    # don't misreport on unexpected status format
                self.rows_written += len(ids)

        # The offline scan runs every round: once a robot stops sending, its queue
        # is empty, and scanning "only the batch that had telemetry" would never
        # flip a stopped robot to false.
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
        """Set the flag and wake run(), returning after the queue backlog lands
        (shutdown order see main.py lifespan)."""
        self._stop = True
        self._stop_event.set()

    @property
    def stats(self) -> dict:
        return {
            "flushes": self.flushes,
            "rows_written": self.rows_written,
            "offline_marks": self.offline_marks,
            "queue_depth": self.queue.qsize(),
            # Fold rate = duplicate count folded away. At 100 robots x 10Hz steady
            # state it should approach (1000 x window_seconds - 100), direct
            # evidence that "batch semantics are in effect".
            "deduped": sum(self.dupes.values()),
        }


metadata_writer = MetadataWriter()
