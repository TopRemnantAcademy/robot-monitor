"""Real-DB regression test for cagg real-time aggregation backfill: the two `/history` data
sources (R-13) + dashboard latest state (R-16).

What it tests
-------------
All three caggs must be **real-time aggregation** (`timescaledb.materialized_only = false`):

  * R-13 / §5.1c: `robot_history_10s` (`/history` short span, `end_offset=10s`)
    and `robot_history_1h` (`/history` long span, `end_offset=1h`);
  * R-16 / §5.1d: `robot_status_10s` (**dashboard latest state**, `end_offset=1min`).

If TimescaleDB 2.13.1's default `materialized_only = true` is in effect, the query view only
covers `[.., now()-end_offset]`, and the `[now()-end_offset, now()]` segment is **unqueryable**
=> a hole at the right edge of `/history` (1h / 10s) and "dashboard latest value lags ~1min".
This test locks "real-time backfill works" into a **failable** signal: the moment someone flips
any one of them back to `materialized_only = true` (or deletes the `ALTER ... SET
(materialized_only = false)` from `infra/init.sql` §5.1c / §5.1d), this test goes red immediately.

Relationship between the two paths
---------------------------------
R-16 only **appends** `robot_status_10s`: the two R-13 ALTERs from §5.1c must be **preserved
verbatim** (this test guards them too; rolling back R-13 also turns red).

Approach (real TimescaleDB, no mock)
------------------------------------
Reuse `timescale/timescaledb:2.13.1-pg16` from `tests/e2e/docker-compose.e2e.yml`
(same version as `infra/docker-compose.yml`; ISSUE #60 explicitly **forbids** mock stand-ins
masquerading as real TimescaleDB):

  1. Start the timescaledb container -> on first boot it auto-runs the mounted `infra/init.sql`
     (the object under test itself, containing R-13's 2x + R-16's 1x
     `ALTER ... SET (materialized_only = false)`);
  2. Verify the three views' `materialized_only` is actually `false` (catalog
     `_timescaledb_catalog.continuous_agg`), and that `pg_get_viewdef` contains the real-time
     segment `UNION ALL ... FROM robot_telemetry` (second corroborating evidence);
  3. Insert `[now()-30min, now()]` data into `robot_telemetry` (1800 rows x 1 robot, 10Hz);
  4. Assert `SELECT count(*) FROM robot_history_1h WHERE bucket >= now()-'90 min' AND robot_id=$1` **> 0**
     **and** `... robot_history_10s ...` **> 0**
     **and** `... robot_status_10s ...` **> 0** (the inserted data must be queryable back via cagg).

Single-test criterion for the third item (R-16)
-----------------------------------------------
`robot_status_10s`'s `end_offset` is **1min**, different from the other two, so its real-time
segment is `[now-1min, now]`. The inserted 1800 rows cover `[now-30s, now]` (= the last 30
seconds, 300 rows at 10Hz; the overall window is still `[now-30min, now]`), which necessarily
falls inside that real-time segment -- exactly what R-16 proves: "dashboard latest state no
longer lags 1min". The query window of 90s (rather than 90min) is **deliberate**: its left edge
crosses the `now-1min` real-time boundary, both to confirm "the real-time segment is queryable
back" and to incidentally cover R-13 §4.3's control criterion "the same query returns 0 when
`=true`". The materialized table has never been refreshed (`WITH NO DATA`), so under `=true`
that window must be 0 => the assertion can fail.

Why insert `[now-30min, now]` but query `[now-90min, now]`
---------------------------------------------------------
The 30min data falls entirely within the two views' **real-time segments** (the real-time segment
= between the materialization watermark and `now()`, transparently backfilled by the engine for
`robot_telemetry`). The 90min query window is chosen to simultaneously cover the boundary of
"materialized segment + real-time segment", ensuring the assertion is about "can the in-window
data be queried back via cagg"; if `materialized_only=true`, the query view only reaches the
materialization watermark (this test never refreshes, so the materialized table is empty) => that
window returns 0 and the assertion goes red.

When Docker is absent
--------------------
**skip and report clearly** (`pytest.skip` / exit code 3), never red. The decision order matches
`test_e2e_30k.py`: is the `docker` command present -> `docker info` (is the daemon reachable) ->
is the `docker compose` plugin available. Each layer gives "why skipped" and "what is needed to
verify", not masking an environment problem as a code failure.

How to run
----------
    # with pytest
    python -m pytest tests/e2e/test_cagg_realtime_backfill.py -v -s
    # without pytest (this file has its own main; exit code 0=pass 1=assertion-fail 3=env-not-ready)
    python tests/e2e/test_cagg_realtime_backfill.py

Relationship with `test_e2e_30k.py`
----------------------------------
In `test_e2e_30k.py`'s e2e stage the stack is already up, so this file's assertion function
`assert_cagg_realtime_backfill()` is also **called once while the stack is alive** ("folded into
the existing e2e", R-13 doc §7): that way CI's `e2e-30k-landing` produces this test's real-DB
evidence without any config change, while this file also keeps an independently runnable entry
point (for easier localization).

Environment variables (all have defaults):
    E2E_COMPOSE=<path>       override compose file path
    E2E_KEEP=1               keep the stack after finishing (for investigation)
    E2E_CAGG_ROBOT_ID        robot ID under test (default humanoid-001)
    E2E_CAGG_ROWS            number of rows to insert (default 1800, covers [now-30min, now])
    E2E_CAGG_STATUS_ROWS     rows dedicated to robot_status_10s insert (default 300 = [now-30s, now])
    E2E_CAGG_UP_TIMEOUT      stack start + health-wait timeout (seconds, default 300)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(os.environ.get(
    "E2E_COMPOSE", ROOT / "tests" / "e2e" / "docker-compose.e2e.yml"))

ROBOT_ID = os.environ.get("E2E_CAGG_ROBOT_ID", "humanoid-001")
# 1800 rows = [now-30min, now] at 10Hz (matches mock_fleet's paid frequency).
ROWS = int(os.environ.get("E2E_CAGG_ROWS", "1800"))
UP_TIMEOUT_S = int(os.environ.get("E2E_CAGG_UP_TIMEOUT", "300"))
KEEP = os.environ.get("E2E_KEEP") == "1"

# Query window: covers the boundary of "materialized segment + real-time segment"; the 30min
# inserted data necessarily falls within it.
QUERY_SPAN = "90 minutes"

# R-16: `robot_status_10s` (dashboard latest state) has `end_offset = 1 minute`, so its
# real-time segment is only `[now-1min, now]`; hence its query window is given separately
# (90s crosses that boundary rather than 90min).
STATUS_QUERY_SPAN = "90 seconds"
# R-16: robot-side insert = `[now-30s, now]` at 10Hz = 300 rows, which falls inside the 1min real-time segment.
STATUS_ROWS = int(os.environ.get("E2E_CAGG_STATUS_ROWS", "300"))

PYTEST_SKIP_MARKER = "E2E-SKIP"

# The three caggs under test. The first two are the two `/history` data sources (R-13, **no rollback**);
# the third is the dashboard latest state (R-16's newly added assertion target).
HISTORY_CAGGS = ("robot_history_10s", "robot_history_1h")
STATUS_CAGG = "robot_status_10s"
TARGET_CAGGS = (*HISTORY_CAGGS, STATUS_CAGG)

# Rows inserted and query window per cagg:
#   history two views: `[now-30min, now]` 1800 rows (10Hz), query `[now-90min, now]`;
#   status view:       `[now-30s, now]`  300 rows (10Hz), query `[now-90s, now]` (only the real-time segment).
CAGG_PLAN = {
    "robot_history_10s": ("90 minutes", ROWS),
    "robot_history_1h": ("90 minutes", ROWS),
    STATUS_CAGG: (STATUS_QUERY_SPAN, STATUS_ROWS),
}


class E2ESkip(Exception):
    """Environment not ready (no Docker, etc.) -- not a code failure; must skip explicitly."""


def run(cmd: list[str], timeout: int, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess with a hard timeout and raw output preserved (failure messages must be pasteable into an Issue)."""
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(cmd)}\n"
            f"exit={proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    return proc


def compose(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args],
               timeout=timeout, check=check)


def require_docker() -> None:
    """skip if no Docker (with a clear reason), never red."""
    if shutil.which("docker") is None:
        raise E2ESkip(
            "docker command not found in PATH. This test needs real TimescaleDB"
            " (timescale/timescaledb:2.13.1-pg16), and **forbids** mock stand-ins (ISSUE #60 forbidden item)."
            " Need: run on a machine/runner with Docker installed."
        )
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "docker daemon not reachable (`docker info` non-zero)."
            f"Raw output: {(probe.stderr or probe.stdout).strip()[:400]}"
        )
    if not COMPOSE_FILE.is_file():
        raise E2ESkip(f"compose file does not exist: {COMPOSE_FILE}")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "`docker compose` (plugin version) unavailable, `docker compose version` non-zero. Raw output: "
            f"{(probe.stderr or probe.stdout).strip()[:400]}"
        )


# ------------------------------------------------------------------ SQL helpers

def _psql(sql: str, timeout: int = 60) -> str:
    """Execute SQL inside the timescaledb container, return a single value (whitespace stripped)."""
    proc = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                   "-d", "robot_monitor", "-tAc", sql, timeout=timeout)
    return proc.stdout.strip()


def wait_db_ready(timeout: int = UP_TIMEOUT_S) -> None:
    """Wait until TimescaleDB is truly usable.

    Waiting only for the container to be healthy is not enough: `init.sql` is executed by the
    docker-entrypoint on **first boot**, so querying caggs before it finishes yields a false
    "view does not exist" failure. Here we wait until the `timescaledb` extension AND all three
    target caggs are in place (the cq.continuous_aggregate catalog has rows).
    """
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        probe = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                        "-d", "robot_monitor", "-tAc",
                        "SELECT count(*) FROM timescaledb_information.continuous_aggregates "
                        "WHERE view_name IN ("
                        + ", ".join(f"'{c}'" for c in TARGET_CAGGS) + ");",
                        check=False, timeout=30)
        if probe.returncode == 0:
            last = probe.stdout.strip()
            if last == str(len(TARGET_CAGGS)):
                return
        else:
            last = (probe.stderr or probe.stdout).strip()[-200:]
        time.sleep(2)
    raise AssertionError(
        f"TimescaleDB not ready within {timeout}s ({len(TARGET_CAGGS)} caggs not all built, "
        f"last probe: {last!r})."
        "Most likely infra/init.sql failed during the container's first boot -- check "
        f"`docker compose -f {COMPOSE_FILE} logs timescaledb`."
    )


# ------------------------------------------------------------------ assertion body

def assert_cagg_realtime_backfill(psql_fn=None, robot_id: str = ROBOT_ID,
                                  rows: int = ROWS, span: str = QUERY_SPAN) -> dict:
    """Assert real-time backfill against a **already-running** real TimescaleDB stack (shared by the
    standalone entry point and test_e2e_30k).

    `psql_fn`: single-value SQL executor (defaults to this file's `_psql`; `test_e2e_30k.py` can
    pass its own `psql` to reuse the same stack that e2e already brought up, avoiding a restart).
    `rows` / `span`: insert-row count and query window for the two `/history` views (preserving
    R-13's existing signature and defaults, so callers need no change); R-16's `robot_status_10s`
    uses `STATUS_ROWS` / `STATUS_QUERY_SPAN` from this file's `CAGG_PLAN` (different 1min
    real-time-segment criterion, see file header).

    Returns an evidence dict (rows inserted, per-view count, measured materialized_only, whether
    viewdef contains the real-time segment).
    """
    q = psql_fn or _psql

    # Rows to insert and query window for each view this round.
    plan = {c: (span, rows) for c in TARGET_CAGGS}
    plan[STATUS_CAGG] = (STATUS_QUERY_SPAN, STATUS_ROWS)

    # 0) Precondition: confirm we are connected to a **real TimescaleDB** (not a PG stand-in;
    # ISSUE #60 forbids mocks).
    version = q("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
    if not version:
        raise AssertionError(
            "Connected DB has no timescaledb extension -- not a real TimescaleDB. "
            "This test forbids mock stand-ins (ISSUE #60)."
        )

    # 1) Attribute evidence: all three views' materialized_only must be false (= real-time aggregation).
    #    This is direct evidence at the "configuration layer": the moment init.sql §5.1c / §5.1d is
    #    deleted or flipped back to true, this goes red first.
    mo = {}
    for cagg in TARGET_CAGGS:
        val = q(
            "SELECT materialized_only FROM _timescaledb_catalog.continuous_agg "
            f"WHERE user_view_name = '{cagg}';"
        )
        mo[cagg] = val
        if val != "f":
            if cagg == STATUS_CAGG:
                hint = (
                    "If 't': init.sql §5.1d's `ALTER MATERIALIZED VIEW IF EXISTS "
                    "robot_status_10s SET (timescaledb.materialized_only = false)` did not take "
                    "effect or was rolled back -- this is exactly the bug R-16 fixes; the dashboard "
                    "latest state would lag ~1min."
                )
            else:
                hint = (
                    "If 't': init.sql §5.1c's `SET (timescaledb.materialized_only = false)` did not "
                    "take effect or was rolled back (R-13 regression) -- a hole appears at the right "
                    "edge of `/history`."
                )
            raise AssertionError(
                f"{cagg}'s materialized_only = {val!r}, expected 'f' (false=real-time aggregation). {hint}"
            )

    # 2) viewdef corroboration: a real-time aggregation query view must contain the real-time segment
    #    `UNION ALL ... FROM robot_telemetry`. A pure materialized view only has
    #    `FROM _materialized_hypertable_N`.
    viewdef_has_realtime = {}
    for cagg in TARGET_CAGGS:
        vd = q(f"SELECT pg_get_viewdef('{cagg}');")
        has_union = "UNION ALL" in vd.upper() and "robot_telemetry" in vd
        viewdef_has_realtime[cagg] = has_union
        if not has_union:
            raise AssertionError(
                f"{cagg}'s pg_get_viewdef does not contain the real-time segment "
                f"`UNION ALL ... FROM robot_telemetry` (materialized_only={mo[cagg]!r}) -- "
                f"meaning it is not a real-time aggregation view."
            )

    # 3) Insert data: insert rows into robot_telemetry (single robot).
    #    Use generate_series to generate inside the DB, avoiding dozens of SQL lines and timestamp
    #    format ambiguity; each payload column takes the minimal legal value satisfying NOT NULL
    #    (JSONB joint columns get '[]').
    #    WARNING: insert each view **separately**. `robot_status_10s`'s real-time segment is only
    #    `[now-1min, now]`; if merged with the 30min history insert, extra unrelated rows appear in
    #    time, making "which segment did the queried-back data come from" indistinguishable -- but
    #    what R-16 must prove is precisely "the most recent 30 seconds of new data can be queried back".
    inserted = {}
    for cagg in TARGET_CAGGS:
        _, n = plan[cagg]
        inserted[cagg] = int(q(
            "WITH ins AS ("
            "  INSERT INTO robot_telemetry ("
            "    time, robot_id, status_code, error_code, battery_pct, max_joint_temp,"
            "    joint_temperatures, joint_positions, joint_torques"
            "  ) SELECT"
            f"    now() - make_interval(secs => (g * 1.0)),"
            f"    '{robot_id}', 1, 0, 88.0, 42.0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb"
            f"  FROM generate_series(0, {n - 1}) AS g"
            "  RETURNING 1"
            ") SELECT count(*) FROM ins;"
        ))
        if inserted[cagg] != n:
            raise AssertionError(
                f"Insert for {cagg} failed: expected {n} rows, actually wrote {inserted[cagg]} rows."
            )

    # 4) Core assertion: all three caggs must return > 0 over their own window (real-time backfill works).
    counts = {}
    for cagg in TARGET_CAGGS:
        s_span, _ = plan[cagg]
        counts[cagg] = int(q(
            f"SELECT count(*) FROM {cagg} "
            f"WHERE bucket >= now() - INTERVAL '{s_span}' AND robot_id = '{robot_id}';"
        ))

    # 5) Underlying cross-check: confirm the data really landed in robot_telemetry (otherwise cagg=0
    #    is an insert problem, not a backfill problem).
    raw = {}
    for cagg in TARGET_CAGGS:
        s_span, _ = plan[cagg]
        raw[cagg] = int(q(
            "SELECT count(*) FROM robot_telemetry "
            f"WHERE time >= now() - INTERVAL '{s_span}' AND robot_id = '{robot_id}';"
        ))

    evidence = {
        "timescaledb_version": version,
        "materialized_only": mo,
        "viewdef_has_realtime": viewdef_has_realtime,
        "inserted": inserted,
        "raw_rows_in_span": raw,
        "cagg_counts": counts,
        "plan": {c: {"span": plan[c][0], "rows": plan[c][1]} for c in TARGET_CAGGS},
        "robot_id": robot_id,
        "query_span": span,
    }

    for cagg, c in counts.items():
        if c <= 0:
            s_span, n = plan[cagg]
            if cagg == STATUS_CAGG:
                hint = (
                    f"materialized_only={mo[cagg]!r} (expected 'f'). If 't': init.sql §5.1d's "
                    "`SET (timescaledb.materialized_only = false)` was deleted/rolled back, "
                    "the dashboard latest state is missing the most recent 1min (exactly what R-16 fixes)."
                )
            else:
                hint = (
                    f"materialized_only={mo[cagg]!r} (expected 'f'). If 't': init.sql §5.1c's "
                    "`SET (timescaledb.materialized_only = false)` was deleted/rolled back, "
                    "a hole appears at the right edge of `/history` (R-13 regression)."
                )
            raise AssertionError(
                f"{cagg} returned 0 on `bucket >= now()-'{s_span}' AND robot_id={robot_id!r}`, "
                f"but the underlying robot_telemetry has {raw[cagg]} rows in the same window "
                f"(this round inserted {n} rows for it)"
                f" -- real-time aggregation backfill **did not take effect**. {hint}"
            )
    return evidence


def render_report(ev: dict) -> str:
    lines = [
        "== cagg real-time backfill regression (real TimescaleDB): R-13 /history x2 + R-16 dashboard latest state ==",
        f"TimescaleDB version: {ev['timescaledb_version']}",
        f"materialized_only: {ev['materialized_only']} (expected 'f' for all three views)",
        f"viewdef contains real-time segment: {ev['viewdef_has_realtime']}",
        f"rows inserted: {ev['inserted']} (robot_id={ev['robot_id']})",
        f"rows in underlying robot_telemetry window: {ev['raw_rows_in_span']}",
        f"cagg counts (per window): {ev['cagg_counts']}; plan={ev['plan']}",
        "Conclusion: **PASS** -- all three caggs are real-time aggregation and query back in-window data (>0).",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ start/stop stack (standalone entry point)

def up_timescaledb() -> None:
    """Only bring up timescaledb (this test needs no emqx/backend/redis, and does not run mock_fleet)."""
    compose("up", "-d", "timescaledb", timeout=UP_TIMEOUT_S)
    wait_db_ready()


def teardown() -> None:
    if KEEP:
        print("[cagg] E2E_KEEP=1, keeping the stack (manual cleanup: "
              f"docker compose -f {COMPOSE_FILE} down -v)", flush=True)
        return
    # down -v: delete volumes too, ensuring the next round is a **brand-new DB + init.sql re-run**.
    compose("down", "-v", "--remove-orphans", check=False, timeout=120)


# ------------------------------------------------------------------ pytest / bare-run entry point

def _pytest_skip(reason: str):
    if under_pytest():
        import pytest  # noqa: PLC0415

        pytest.skip(reason)
    print(f"{PYTEST_SKIP_MARKER}: {reason}", flush=True)
    sys.exit(3)


def under_pytest() -> bool:
    return "pytest" in sys.modules and __name__ != "__main__"


def test_cagg_realtime_backfill() -> None:
    try:
        require_docker()
    except E2ESkip as exc:
        _pytest_skip(str(exc))

    try:
        up_timescaledb()
        ev = assert_cagg_realtime_backfill()
        print("\n" + render_report(ev) + "\n", flush=True)
    finally:
        teardown()


def main() -> int:
    """Entry point without pytest: 0=pass 1=assertion-fail 3=env-not-ready (skip)."""
    try:
        require_docker()
    except E2ESkip as exc:
        print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
        return 3

    try:
        up_timescaledb()
    except AssertionError as exc:
        print(f"[cagg] stack-up/ready failed: {exc}", flush=True)
        _dump_logs()
        teardown()
        return 1

    try:
        ev = assert_cagg_realtime_backfill()
    except AssertionError as exc:
        print(f"[cagg] assertion failed: {exc}", flush=True)
        _dump_logs()
        teardown()
        return 1

    print("\n" + render_report(ev), flush=True)
    teardown()
    return 0


def _dump_logs() -> None:
    """Failure evidence: grab the tail of the timescaledb container log (init.sql errors show up here)."""
    try:
        proc = compose("logs", "--tail=80", "timescaledb", timeout=30, check=False)
        print("\n===== compose logs timescaledb (tail 80) =====", flush=True)
        print((proc.stdout or "")[-8000:] or "(no output)", flush=True)
    except Exception as exc:  # evidence collection itself must never alter the exit code
        print(f"(evidence collection failed: {exc})", flush=True)


if __name__ == "__main__":
    sys.exit(main())
