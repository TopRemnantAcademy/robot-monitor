"""`/history` data-source switching and fallback (ISSUE #76).

Why a separate file, and **no** dependence on a real TimescaleDB: one acceptance point in this
issue is the easiest to write off as "passes by eyeball comparison" -- "changed the continuous
aggregate but missed a column". Order 16 already really crashed once (missed `joint_positions` ->
the Drawer's third line chart was constantly empty), recorded in STATUS.md section 6 item 2. So
here we make three things into **machine-decidable** assertions:

1. The SELECT column sets of the aggregate branch and the fallback branch are **identical verbatim**
   -- otherwise one side adds a column and the other misses it, producing an intermittent fault of
   "normal normally, but empty again once you refresh the window", harder to find than total
   breakage;
2. `joint_positions / joint_temperatures / joint_torques` are in the columns of **both** branches
   (ISSUE #76 constraint: must not remove `last(joint_positions, time)`);
3. When going through the aggregate, no query to the raw table, and fallback only when the aggregate
   returns empty -- use a countable fake connection to lock the call sequence, and assert the
   `source` count label in sync (a p95 drop must be attributable to "really switched data source").

Using a fake pool rather than a real PG: this environment has no TimescaleDB, so the real DB can't
be tested; but what we want to verify here is **control flow and the SQL contract**, both decidable
at the pure-Python layer. The real SQL going through is documented separately by R-09 section 4's
measured records and the PR's "unverified" section.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re

import pytest

os.environ.setdefault("JWT_SECRET", "test-only-secret-0123456789abcdef")

from app import main as main_mod  # noqa: E402

# The three JSONB joint columns that must appear in both branches (ISSUE #76 acceptance /
# Order 16 incident regression).
JOINT_COLS = ("joint_temperatures", "joint_positions", "joint_torques")


def _select_body(sql: str) -> str:
    """Take the column list between SELECT and FROM (whitespace-normalized), for verbatim
    comparison of the two branches."""
    m = re.search(r"SELECT(.*?)\n\s*FROM", sql, re.S)
    assert m, f"SQL has no SELECT ... FROM: {sql!r}"
    # strip SQL comments (`-- ...` to end of line); they only appear in the raw-table branch, and
    # would cause false reds if included in the comparison.
    body = re.sub(r"--[^\n]*", "", m.group(1))
    return re.sub(r"\s+", " ", body).strip()


class _FakeConn:
    """A fake connection that returns results per a preset script, while recording every SQL
    actually executed."""

    def __init__(self, script: list[list[dict]]):
        self._script = list(script)
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        assert self._script, "fetch was called more times than the preset script"
        return self._script.pop(0)


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _row(bucket: _dt.datetime) -> dict:
    return {
        "bucket": bucket,
        "robot_id": "humanoid-001",
        "status_code": 1,
        "battery_pct": 88.5,
        "max_joint_temp": 51.2,
        "joint_temperatures": {"left_knee_pitch": 49.1},
        "joint_positions": {"left_knee_pitch": 0.5},
        "joint_torques": {"left_knee_pitch": 3.4},
    }


def _run_history(monkeypatch, conn, minutes=5, resolution="10s"):
    """Run one `/history` against the fake pool, return (response dict, fake connection).

    `minutes` / `resolution` can be overridden (R-12 needs to trigger the hourly path by span);
    default values are kept, so existing call sites keep their semantics.
    """
    from app.db import session as session_mod

    async def _fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(session_mod, "get_pool", _fake_get_pool)
    return asyncio.run(
        main_mod.history(
            "humanoid-001", minutes=minutes, resolution=resolution, _user=None
        )
    )


@pytest.fixture(autouse=True)
def _no_metrics_noise(monkeypatch):
    """This file only exercises control flow, not the real latency of the Histogram observation."""
    monkeypatch.setattr(main_mod.metrics.history_latency_seconds, "observe", lambda *a: None)


class TestSqlContract:
    """Pure SQL-text assertions -- run without any fake, the fastest guardrail."""

    def test_all_joint_columns_in_both_branches(self):
        """The three joint columns must appear in **both** branches simultaneously -- this is the
        core invariant of this issue."""
        for name, sql in _live_sqls().items():
            body = _select_body(sql)
            for col in JOINT_COLS:
                assert f"last({col}, time) AS {col}" in body, (
                    f"{name} branch missing `last({col}, time) AS {col}`;"
                    f" actual column list: {body}"
                )

    def test_branch_columns_identical_apart_from_bucket_expr(self):
        """Apart from the bucket **expression**, the two branches must have verbatim-identical
        column lists.

        Why we exclude the bucket: the two branches' bucket sources already differ -- the aggregate
        branch reads the materialized column `bucket`, the raw-table branch must compute
        `time_bucket(...)` on the fly. This is **by design**, not drift. What we must truly lock down
        is "every other column, every aggregate function, every alias is identical", otherwise one
        side adds a column and the other misses it, producing the intermittent fault of "normal
        normally, but empty again once you refresh the window". At the same time we separately assert
        that the bucket column is aliased to `bucket` in both branches (the response field name
        depends on it).
        """
        sqls = _live_sqls()
        stripped = {}
        for name, sql in sqls.items():
            body = _select_body(sql)
            # Replace the bucket column's **expression** with a neutral literal, keeping only the
            # alias. Non-greedy match up to `AS bucket`, avoiding nested parens inside time_bucket(...).
            body = re.sub(
                r"^.*?\sAS bucket",
                "BUCKET_EXPR AS bucket",
                body,
                count=1,
            )
            stripped[name] = body
        assert stripped["cagg"] == stripped["raw"], (
            "aggregate branch and fallback branch columns drifted (bucket expression neutralized):\n"
            f"  cagg: {stripped['cagg']}\n  raw : {stripped['raw']}"
        )
        for name, body in stripped.items():
            assert body.startswith("BUCKET_EXPR AS bucket"), f"{name} branch bucket not aliased to bucket"

    def test_cagg_branch_reads_the_continuous_aggregate(self):
        assert "FROM robot_history_10s" in _live_sqls()["cagg"]

    def test_cagg_branch_does_not_time_bucket_again(self):
        """The aggregate is already a 10s bucket; wrapping another time_bucket is duplicate
        aggregation (and hides the index)."""
        assert "time_bucket" not in _live_sqls()["cagg"]

    def test_cagg_branch_filters_on_bucket_column(self):
        """The filter must land on the materialized column `bucket` to use the (robot_id, bucket)
        index auto-built by the continuous aggregate."""
        sql = _live_sqls()["cagg"]
        assert "bucket >= now()" in sql

    def test_raw_branch_still_time_buckets(self):
        assert "time_bucket" in _live_sqls()["raw"]
        assert "FROM robot_telemetry" in _live_sqls()["raw"]


def _live_sqls() -> dict:
    """Get the two SQL statements actually used at /history runtime.

    To avoid the self-deception of "the test re-creates the SQL string, and product code writes its
    own copy", here we directly run /history once and pull the real SQL from the fake connection's
    records.
    """
    return _captured_sqls


# Module-level cache: after import, run one empty-result /history to capture the two real SQLs.
class _CaptureConn:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        return []  # empty result -> triggers fallback -> both SQL statements get called


_cap = _CaptureConn()
_captured_sqls: dict = {}


def _prime_sqls():
    from app.db import session as session_mod

    orig = session_mod.get_pool

    async def _fake():
        return _FakePool(_cap)

    session_mod.get_pool = _fake
    try:
        asyncio.run(main_mod.history("humanoid-001", minutes=5, resolution="10s", _user=None))
    finally:
        session_mod.get_pool = orig
    assert len(_cap.calls) == 2, f"expected aggregate + fallback once each, actual {len(_cap.calls)} times"
    _captured_sqls["cagg"] = _cap.calls[0]
    _captured_sqls["raw"] = _cap.calls[1]


_prime_sqls()


class TestRouting:
    """Control flow: when we hit the aggregate, when we fall back, and whether the source count can
    be attributed."""

    def test_cagg_hit_skips_raw_table(self, monkeypatch):
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        before = _counter("cagg")
        resp = _run_history(monkeypatch, conn)

        assert len(conn.calls) == 1, "must not query the raw table when the aggregate is hit"
        assert "robot_history_10s" in conn.calls[0]
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}
        assert _counter("cagg") == before + 1

    def test_empty_cagg_falls_back_to_raw(self, monkeypatch):
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[], [_row(now)]])  # aggregate empty -> fallback has data
        before_raw = _counter("raw")
        resp = _run_history(monkeypatch, conn)

        assert len(conn.calls) == 2, "must fall back to the raw table when the aggregate is empty"
        assert "robot_telemetry" in conn.calls[1]
        assert _counter("raw") == before_raw + 1, "fallback must be counted, otherwise p95 cannot be attributed"
        # The joint_positions on the fallback path must not be lost (ISSUE #76 / Order 16 incident)
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}

    def test_both_empty_returns_empty_points_not_error(self, monkeypatch):
        """A robot that "never reported data" is normal semantics (main.py's original comment), not a
        5xx."""
        conn = _FakeConn([[], []])
        resp = _run_history(monkeypatch, conn)
        assert resp["points"] == []

    def test_query_failure_is_503_and_not_counted_as_cagg(self, monkeypatch):
        """A failed aggregate query -> 503, and **must not** be counted as source="cagg".

        Why test this separately: if `source` were also recorded as cagg on the exception path, the
        most important failure to expose (the aggregate can't be queried) would be disguised as the
        most ideal state (aggregate hit), looking good on the panel. Here we pin this counter-example
        by the before/after delta.
        """

        class _BoomConn:
            async def fetch(self, sql, *args):
                raise RuntimeError("boom")

        before_cagg = _counter("cagg")
        resp = _run_history(monkeypatch, _BoomConn())
        assert resp.status_code == 503
        assert _counter("cagg") == before_cagg, "failure path must not be counted as cagg"

    def test_invalid_resolution_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=5, resolution="500ms", _user=None)
            )
        assert ei.value.status_code == 400


def _counter(source: str) -> float:
    return main_mod.metrics.history_source.labels(source=source)._value.get()


# ───────────────────────── R-12: hourly downsampling ─────────────────────────
#
# R-12's core assertion: once `minutes` exceeds the threshold we **switch data source** (not just
# parameters). Same structure as TestSqlContract above; here we check that "the hourly-path SQL
# points at robot_history_1h, the column set matches the 10s path, the response resolution echoes
# 3600", plus the upper bound 400.
#
# Why a fake pool rather than a real DB: this environment has no TimescaleDB (same as file header
# note), and "which table to route to by span" is pure control flow, decidable verbatim at the
# Python layer. The semantic correctness of the real SQL is documented by the e2e 30k run-through +
# the PR's "unverified" section.
def _capture_hourly_sql() -> dict:
    """Run one large-span /history, capture the two SQL statements the hourly path actually uses
    (aggregate + fallback)."""
    conn = _FakeConn([[], []])  # both empty -> aggregate and fallback each called once
    from app.db import session as session_mod

    orig = session_mod.get_pool

    async def _fake():
        return _FakePool(conn)

    session_mod.get_pool = _fake
    try:
        asyncio.run(
            main_mod.history("humanoid-001", minutes=1440, resolution="10s", _user=None)
        )
    finally:
        session_mod.get_pool = orig
    assert len(conn.calls) == 2, f"expected aggregate + fallback once each, actual {len(conn.calls)} times"
    return {"cagg": conn.calls[0], "raw": conn.calls[1]}


class TestHourlyRouting:
    """R-12 T2: span routing + column completeness + resolution echo + upper bound."""

    def test_large_span_routes_to_hourly_aggregate(self, monkeypatch):
        """minutes=1440 (24h) -> reads robot_history_1h, no longer reads the 10s view (V3)."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440)

        assert len(conn.calls) == 1, "must not query the raw table when the hourly aggregate is hit"
        assert "robot_history_1h" in conn.calls[0]
        assert "robot_history_10s" not in conn.calls[0], "large span should not read the 10s view again"

    def test_small_span_still_routes_to_10s(self, monkeypatch):
        """Within the threshold (minutes=180) still goes to 10s -- R-03 section 4 contract hard red
        line, must not be squeezed out by the hourly path."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        _run_history(monkeypatch, conn, minutes=180)

        assert "robot_history_10s" in conn.calls[0]
        assert "robot_history_1h" not in conn.calls[0]

    def test_hourly_branch_keeps_joint_positions(self, monkeypatch):
        """V4: the hourly-path response contains joint_positions (R-12 section 6 hard red line, the
        Drawer's third line chart)."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440)

        p = resp["points"][0]
        assert p["joint_positions"] == {"left_knee_pitch": 0.5}
        assert p["joint_temperatures"] == {"left_knee_pitch": 49.1}
        assert p["joint_torques"] == {"left_knee_pitch": 3.4}

    def test_hourly_resolution_echoed_as_3600s(self, monkeypatch):
        """The hourly path echoes resolution as 3600s (the real bucket seconds), not the input 10s.

        Echoing the input would make the frontend draw a 1h bucket as a 10s bucket -- a 360x
        difference on the x-axis interval.
        """
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440, resolution="30s")
        assert resp["resolution"] == "3600s"

    def test_small_span_resolution_still_echoes_input(self, monkeypatch):
        """Small path echoes the input resolution (verbatim, R-03 regression)."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=5, resolution="30s")
        assert resp["resolution"] == "30s"

    def test_hourly_empty_falls_back_to_raw(self, monkeypatch):
        """T3: when the hourly aggregate is empty, fall back to the raw table too, to avoid
        cold-start blanks (reuses the existing fallback semantics)."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[], [_row(now)]])
        before_raw = _counter("raw")
        resp = _run_history(monkeypatch, conn, minutes=1440)

        assert len(conn.calls) == 2, "must fall back to the raw table when the hourly aggregate is empty"
        assert "robot_telemetry" in conn.calls[1]
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}
        assert _counter("raw") == before_raw + 1

    def test_hourly_columns_match_10s_columns(self):
        """The hourly path and the 10s path have **verbatim-identical** SELECT column sets (bucket
        expression neutralized).

        The two views are structurally mirrored (init.sql section 5.1b); once they drift, the hourly
        response loses columns.
        """
        hourly = _select_body(_capture_hourly_sql()["cagg"])
        ten_s = _select_body(_live_sqls()["cagg"])

        def _neutralize(body: str) -> str:
            return re.sub(r"^.*?\sAS bucket", "BUCKET_EXPR AS bucket", body, count=1)

        assert _neutralize(hourly) == _neutralize(ten_s), (
            "hourly path and 10s path column drift:\n"
            f"  hourly: {hourly}\n  10s   : {ten_s}"
        )

    def test_hourly_branch_keeps_all_joint_columns(self):
        """The three JSONB joint columns must be present in the hourly path (including fallback)."""
        for name, sql in _capture_hourly_sql().items():
            body = _select_body(sql)
            for col in JOINT_COLS:
                assert f"last({col}, time) AS {col}" in body, f"{name} branch missing {col}"

    def test_hourly_branch_reads_materialized_bucket(self):
        """The hourly path reads the materialized bucket column (no time_bucket wrapper), so it can
        use the auto-built index."""
        sql = _capture_hourly_sql()["cagg"]
        assert "time_bucket" not in sql
        assert "bucket >= now()" in sql


class TestMinutesUpperBound:
    """R-12 T2 / V5: minutes upper bound 400."""

    def test_over_limit_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history(
                    "humanoid-001",
                    minutes=main_mod.MAX_HISTORY_MINUTES + 1,
                    resolution="10s",
                    _user=None,
                )
            )
        assert ei.value.status_code == 400
        assert ei.value.detail == "history span too large"

    def test_two_hundred_thousand_minutes_is_400(self):
        """Per requirement section 5 V5: ?minutes=20000 -> 400 (20000 > 10080)."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=20000, resolution="10s", _user=None)
            )
        assert ei.value.status_code == 400
        assert ei.value.detail == "history span too large"

    def test_at_limit_is_not_rejected_by_bound(self, monkeypatch):
        """Exactly at the upper bound (10080) must not be 400 -- the boundary must be >, not >=."""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=main_mod.MAX_HISTORY_MINUTES)
        assert resp["resolution"] == "3600s"  # 10080 > 180 -> hourly path
        assert resp["points"]

    def test_bound_rejected_before_pool_acquired(self, monkeypatch):
        """The upper-bound check must happen before acquiring a connection -- otherwise the
        full-table scan has already been handed to the DB before the 400 is reported.

        We use a pool that "asserts failure the moment it acquires" to pin down the order.
        """
        from fastapi import HTTPException

        class _NoAcquirePool:
            def acquire(self):
                raise AssertionError("should not acquire a connection before the upper-bound check")

        from app.db import session as session_mod

        async def _fake():
            return _NoAcquirePool()

        monkeypatch.setattr(session_mod, "get_pool", _fake)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=20000, resolution="10s", _user=None)
            )
        assert ei.value.status_code == 400
