"""G-6: `robot_metadata.last_seen / is_online` is **really written** (ISSUE #61).

Why a standalone group of cases (instead of tacking it into test_emit_order.py):

This table's write path is **unique** — the 1000 msg/s telemetry ingest side deliberately does
not write it (`backend_ingestion/app/services/liveness.py:23` reasons: a per-row UPDATE is 1000
transactions/second, unacceptable), and instead the alert engine ticker low-frequency-persists on
**state change**, gated by `alert_engine/app/main.py` ticker's
`online_state.get(rid) is not online`.

When this path breaks, **the dashboard won't error**: it just keeps showing green using the
backend identity view's computed online count, and nobody notices robot_metadata has stopped
updating. So red-capable assertions are needed.

Three assertions, none dispensable:

1. The SQL really hits `robot_metadata` with the correct `$1/$2` parameter order —
   asserting merely "a function was called" equals no test: changing the table name to
   robot_status would still go all green.
2. When the DB throws, it must be **swallowed** (`log.exception`), not take down the whole ticker —
   one PG hiccup taking down the ticker means all offline judgements stop, far worse than
   "metadata is momentarily slightly stale".
3. **Low frequency**: the ticker is a per-second tight loop; once the gate fails it's 100 UPDATEs/second.
   This case sets `tick_interval_s` to 0 and runs hundreds of rounds, asserting exactly 1 write
   per robot per transition.
"""

from __future__ import annotations

import asyncio
import time

from tests.conftest import run

from app.config import settings
from app import store


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class _RecConn:
    def __init__(self, sink: list):
        self._sink = sink

    async def execute(self, sql: str, *args):
        self._sink.append((sql, args))


class _RecPool:
    def __init__(self, sink: list):
        self._sink = sink

    def acquire(self):
        return _AsyncCtx(_RecConn(self._sink))


class _BoomPool:
    """`acquire()` throws immediately: simulates connection-pool exhaustion / PG network drop."""

    def acquire(self):
        raise ConnectionError("injected: pool exhausted")


def _patched_get_pool(pool):
    async def _get_pool():
        return pool

    return _get_pool


# ------------------------------------------------- 1. SQL lands in the right place

def test_update_online_writes_the_metadata_row(monkeypatch):
    """Must UPDATE robot_metadata, with `is_online=$2` / `WHERE robot_id=$1` order correct.

    When the parameter order is reversed the statement still executes successfully (it only errors
    on a type mismatch, and here both columns happen to avoid that combination), silently writing
    wrong data into the right table — only asserting the parameter tuple can stop it."""
    sink: list = []
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_RecPool(sink)))

    run(store.update_online("humanoid-042", True))

    assert len(sink) == 1, f"expected 1 UPDATE, actual {len(sink)}"
    sql, args = sink[0]
    assert "UPDATE robot_metadata" in sql
    assert "SET is_online=$2" in sql
    assert "last_seen=NOW()" in sql
    assert "WHERE robot_id=$1" in sql
    assert args == ("humanoid-042", True), f"wrong parameter tuple: {args}"


def test_update_online_writes_false_on_offline_transition(monkeypatch):
    """Going offline must not lose the write: `$2` must be False, not silently eaten by an upstream default."""
    sink: list = []
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_RecPool(sink)))

    run(store.update_online("humanoid-007", False))

    assert sink[0][1] == ("humanoid-007", False)


# ------------------------------------------------- 2. DB blow-up must not take down the ticker

def test_update_online_swallows_db_errors(monkeypatch):
    """A metadata write failure only logs: it's not the alert chain and shouldn't have the power
    to take down the whole ticker."""
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_BoomPool()))

    # Not raising out is a pass (store.py's `except Exception: log.exception(...)`)
    run(store.update_online("humanoid-001", True))


# ------------------------------------------------- 3. gate: write only on state change

def test_ticker_writes_metadata_once_per_transition(monkeypatch):
    """Primary assertion: the ticker runs hundreds of rounds, and each robot should write exactly
    once per state transition.

    Under `tick_interval_s=0` the ticker is a tight loop; if the `online_state` gate were removed
    by a later change, calls would become 100×rounds (thousands), and this goes red immediately.

    Deliberately silence the offline rule (phase 1 sets `last_seen` to now) to focus the observation
    on the metadata write path itself; the alert's emit semantics is not this case's concern
    (see test_emit_order.py)."""
    import app.main as m

    calls: list = []
    emitted: list = []

    async def fake_update_online(robot_id: str, online: bool) -> None:
        calls.append((robot_id, online))

    async def fake_emit(alert) -> None:
        emitted.append(alert)

    monkeypatch.setattr(m, "update_online", fake_update_online)
    monkeypatch.setattr(m, "emit", fake_emit)
    monkeypatch.setattr(settings, "tick_interval_s", 0)

    ids = list(settings.robot_ids)
    m.online_state.clear()
    m.windows.clear()
    m.last_seen.clear()

    now = time.time()
    for rid in ids:
        m.last_seen[rid] = now                        # phase 1: all online

    async def drive():
        task = asyncio.create_task(m.ticker())
        await asyncio.sleep(0.2)                      # hundreds of rounds: online state stable, no repeat writes
        phase1 = len(calls)
        # phase 2: all time out → offline transition, each robot should get its 2nd write, and only one
        for rid in ids:
            m.last_seen[rid] = now - settings.offline_after_s * 2
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # spawn() uses create_task; after canceling the ticker give those tasks a bit of CPU to finish
        await asyncio.sleep(0.05)
        return phase1

    try:
        phase1 = run(drive())
    finally:
        m.online_state.clear()
        m.windows.clear()
        m.last_seen.clear()

    assert phase1 == len(ids), (
        f"online phase wrote {phase1} times, expected exactly 1 per robot (total {len(ids)}) —— "
        "online_state gate appears broken, re-sending UPDATE every round"
    )
    assert {rid for rid, _ in calls} == set(ids), "some robot never wrote robot_metadata"
    assert all(online is True for _, online in calls[: len(ids)]), "online phase should write True"

    # phase 2: each robot +1 (no more, no less), value must be False
    assert len(calls) == 2 * len(ids), (
        f"after phase 2 wrote {len(calls)} times, expected {2 * len(ids)} —— transition gate re-fired"
    )
    assert all(online is False for _, online in calls[len(ids):]), "offline phase should write False"


# ------------------------------------------------- 4. read path (ISSUE #96)

def test_load_online_state_reads_is_online_column(monkeypatch):
    """`load_online_state()` must really `SELECT robot_id, is_online`, and parse row by row.

    This is the core read path of #96: the ticker relies on it for the criterion every round.
    Assertions land on the SQL text and return value — asserting merely "a function was called"
    equals no test (changing the column name would still go green)."""
    sink: list = []

    class _RowPool:
        class _Conn:
            async def fetch(self, sql, *args):
                sink.append((sql, args))
                return [
                    {"robot_id": "humanoid-001", "is_online": True},
                    {"robot_id": "humanoid-002", "is_online": False},
                ]

        def acquire(self):
            return _AsyncCtx(self._Conn())

    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_RowPool()))

    out = run(store.load_online_state())

    assert out == {"humanoid-001": True, "humanoid-002": False}
    assert len(sink) == 1
    sql = sink[0][0]
    assert "SELECT robot_id, is_online FROM robot_metadata" in sql


def test_load_online_state_returns_empty_on_db_failure(monkeypatch):
    """A read failure must return an empty dict (rather than raise): the caller falls back to the
    time-delta judgement on "column unavailable"."""
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_BoomPool()))

    out = run(store.load_online_state())

    assert out == {}, "on read failure must not raise, nor fabricate an online state"


def test_ticker_passes_is_online_from_db_to_rule(monkeypatch):
    """The ticker must pass the read `is_online` into the rule (not only pass the in-memory last_seen).

    Method: fix `load_online_state` to a constant, swap the rule for a stub that records its input,
    run exactly one ticker round, and assert the `is_online` field in the state the rule received
    matches the DB."""
    import app.main as m

    seen: list = []

    class _RuleStub:
        def evaluate(self, state, window):
            seen.append(state)
            return None

    async def fake_load():
        return {"humanoid-001": False}

    monkeypatch.setattr(m, "load_online_state", fake_load)
    monkeypatch.setattr(m, "OFFLINE_RULE", _RuleStub())
    monkeypatch.setattr(type(m.settings), "robot_ids",
                        property(lambda self: ["humanoid-001"]))
    monkeypatch.setattr(m.settings, "tick_interval_s", 0.2)

    async def one_round():
        task = asyncio.create_task(m.ticker())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if m._tasks:
            await asyncio.gather(*list(m._tasks), return_exceptions=True)

    m.last_seen.clear()
    m.online_state.clear()
    run(one_round())

    assert seen, "ticker passed no state to the offline rule"
    assert all("is_online" in s for s in seen), f"state received by rule missing is_online: {seen}"
    assert seen[0]["is_online"] is False, f"is_online not passed through from DB: {seen[0]}"
