"""ISSUE #96 -- acceptance of robot_metadata heartbeat's **batch semantics** (pure
in-memory, zero external dependencies).

Covers two forbidden items and two semantic requirements, all assertions landing on
MetadataWriter's real behavior:

1. **No per-row UPDATE** -- 1000 telemetry records (100 robots x 10 times) must fold into a
   single UPDATE statement, with exactly 100 robot_ids in the parameter array (folded by primary key).
2. **No alteration of init.sql column definitions** -- the SQL text only uses `is_online` /
   `last_seen` two columns, no invented third column, no DDL.
3. **Periodic set-to-false** uses `state_ttl_seconds`, not a separate magic number.
4. **When the switch is off, nothing is written**: under `metadata_heartbeat_enabled=False`
   flush() issues no statement at all, guaranteeing the constraint "must not change the existing
   telemetry write path" is verifiable.

Run: `pytest backend_ingestion/tests/` (settings.jwt_secret injected by conftest).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from app.services import metadata_writer as mw
from app.services.metadata_writer import MARK_OFFLINE_SQL, UPDATE_SQL, MetadataWriter


class _FakeConn:
    """A fake connection that records every issued statement (no real DB)."""

    def __init__(self, offline_rows=None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._offline_rows = offline_rows or []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        # asyncpg's UPDATE status string looks like "UPDATE 7"
        return f"UPDATE {len(args[0]) if args else 0}"

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return [{"robot_id": r} for r in self._offline_rows]


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _writer_with(monkeypatch, offline_rows=None) -> tuple[MetadataWriter, _FakeConn]:
    conn = _FakeConn(offline_rows)

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(mw, "get_pool", fake_get_pool)
    return MetadataWriter(), conn


def test_1000_telemetry_folds_into_one_update(monkeypatch):
    """1000 telemetry records (100 robots x 10Hz x 1s) -> **1** UPDATE, only 100 robot_ids."""
    w, conn = _writer_with(monkeypatch)
    for _ in range(10):
        for i in range(1, 101):
            w.put(f"humanoid-{i:03d}")

    asyncio.run(w.flush())

    updates = [(sql, args) for sql, args in conn.calls if "unnest" in sql]
    assert len(updates) == 1, f"should fold into 1 UPDATE, actual {len(updates)}"
    ids, seen = updates[0][1]
    assert len(ids) == 100, f"robot_id should fold to 100, actual {len(ids)}"
    assert len(seen) == 100
    assert all(isinstance(v, datetime) and v.tzinfo is not None for v in seen)
    assert w.stats["deduped"] == 900, "folded-away duplicate count should be 900"
    assert w.stats["rows_written"] == 100


def test_telemetry_count_does_not_change_statement_count(monkeypatch):
    """Frequency does not amplify write amplification: 10 vs 10000 telemetry records both yield
    exactly 1 UPDATE statement."""
    counts = []
    for n in (10, 10000):
        w, conn = _writer_with(monkeypatch)
        for i in range(n):
            w.put(f"humanoid-{(i % 100) + 1:03d}")
        asyncio.run(w.flush())
        counts.append(sum(1 for sql, _ in conn.calls if "unnest" in sql))
    assert counts == [1, 1], f"statement count grew with row count: {counts}"


def test_offline_sweep_uses_state_ttl_seconds(monkeypatch):
    """The threshold for periodic set-to-false must come from settings.state_ttl_seconds
    (default 10s)."""
    from app.config import settings

    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-007"])
    asyncio.run(w.flush())

    sweeps = [(sql, args) for sql, args in conn.calls if "is_online = FALSE" in sql]
    assert len(sweeps) == 1, "each flush should run exactly one offline scan"
    assert sweeps[0][1][0] == float(settings.state_ttl_seconds)
    assert w.stats["offline_marks"] == 1


def test_next_flush_still_sweeps_for_offline(monkeypatch):
    """An empty window must still be scanned: after a robot stops sending telemetry the queue
    is empty, and missing the scan means it is never set to false."""
    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-001", "humanoid-002"])
    asyncio.run(w.flush())                    # empty queue
    assert sum(1 for sql, _ in conn.calls if "is_online = FALSE" in sql) == 1
    assert w.stats["rows_written"] == 0


def test_sql_only_writes_existing_columns():
    """Forbidden to alter init.sql column definitions -> only is_online / last_seen may be written."""
    # The SET clause of the heartbeat statement
    sets = re.findall(r"SET\s+(.+?)\s+FROM\s", UPDATE_SQL, flags=re.IGNORECASE | re.DOTALL)
    assert len(sets) == 1, f"heartbeat UPDATE should have exactly one SET clause: {UPDATE_SQL}"
    written = {c.split("=")[0].strip() for c in sets[0].split(",")}
    assert written == {"last_seen", "is_online"}, f"wrote a new column: {written}"

    sets = re.findall(r"SET\s+(.+?)\s+WHERE\s", MARK_OFFLINE_SQL, flags=re.IGNORECASE | re.DOTALL)
    assert [s.strip() for s in sets] == ["is_online = FALSE"]

    for sql in (UPDATE_SQL, MARK_OFFLINE_SQL):
        assert "robot_metadata" in sql
        assert "ALTER TABLE" not in sql.upper(), "DDL changes forbidden"


def test_heartbeat_uses_server_clock_when_ts_missing(monkeypatch):
    """When payload has no ts, must fall back to the local clock, not write 1970 or None."""
    w, conn = _writer_with(monkeypatch)
    w.put("humanoid-001", None)
    asyncio.run(w.flush())
    _, seen = next((sql, a) for sql, a in conn.calls if "unnest" in sql)[1]
    assert seen[0].year >= 2024, f"timestamp anomaly: {seen[0]!r}"


def test_heartbeat_ignores_stale_payload_ts(monkeypatch):
    """**Regression**: when the payload ts lags behind the server, `last_seen` must still be the
    server's current moment.

    The actual failure observed during development: if `put(rid, ts_ms)` writes the payload
    timestamp as `last_seen`, a client with a lagging clock / replayed history / offline-buffered
    send would make the `MARK_OFFLINE_SQL` predicate `last_seen < NOW() - ttl` flip the row it
    just set TRUE right back to FALSE in the same flush -- "5 robots just reported, then a single
    statement judges all offline".

    Here we reproduce with a 2023-11 timestamp (far older than now-10s): the assertion is that the
    stored timestamp is **now**. If the implementation regresses to `ts_ms / 1000.0`,
    `seen[0].year` would become 2023 and turn red.
    """
    import time as _time
    from datetime import datetime, timezone

    w, conn = _writer_with(monkeypatch)
    stale_ms = 1700000000000                      # 2023-11-14, certainly > ttl
    w.put("humanoid-001", stale_ms)
    asyncio.run(w.flush())

    _, seen = next((sql, a) for sql, a in conn.calls if "unnest" in sql)[1]
    age = _time.time() - seen[0].timestamp()
    assert seen[0].year >= 2024, f"wrote the payload's stale timestamp: {seen[0]!r}"
    assert abs(age) < 5, f"last_seen deviates from server now by {age:.1f}s (should be ~0)"
    assert seen[0] != datetime.fromtimestamp(stale_ms / 1000.0, tz=timezone.utc)


def test_stop_flushes_pending_before_return(monkeypatch):
    """On shutdown nothing in-flight may be lost: after stop() run() exits, but the backlog must
    already be persisted."""
    w, conn = _writer_with(monkeypatch)
    w.put("humanoid-001")

    async def scenario():
        task = asyncio.create_task(w.run())
        await w.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert w.stats["rows_written"] == 1, "heartbeats enqueued before stop() were lost"
    assert w.stats["flushes"] >= 1


def test_disabled_switch_writes_nothing(monkeypatch):
    """Switch off -> flush() issues no statement (guarantees the semantics and throughput of the
    existing telemetry write path are unchanged)."""
    from app.config import settings

    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-001"])
    monkeypatch.setattr(settings, "metadata_heartbeat_enabled", False)

    w.put("humanoid-001")
    asyncio.run(w.flush())

    assert conn.calls == [], f"statements still issued while switch off: {conn.calls}"
    assert w.stats["rows_written"] == 0
    assert w.stats["offline_marks"] == 0


def test_disabled_switch_makes_run_return_immediately(monkeypatch):
    """Switch off -> run() exits immediately (no need to wait for the stop() before close_pool)."""
    from app.config import settings

    w, _ = _writer_with(monkeypatch)
    monkeypatch.setattr(settings, "metadata_heartbeat_enabled", False)

    async def scenario():
        await asyncio.wait_for(w.run(), timeout=1)

    asyncio.run(scenario())      # passes only if not timed out
