"""End-to-end 30,000-row landing verification harness (ISSUE #60).

What it tests
-------------
`STATUS.md` §9 "still unverified" and `docs/requirements/R-09-database-integration.md` §6
both carry one item: **end-to-end 30,000 rows (incl. MQTT/simulator) unverified**.
Existing evidence does not cover this full chain:
  - `tests/test_schema_contract.py`, `backend_ingestion/tests/*` are **unit-test scope**;
    they do not connect MQTT nor a real TimescaleDB;
  - `test_integration.sh` (Order 06) runs bare on the host and **exits entirely without Docker**,
    with no "CI-runnable / skip-without-Docker" branch.
This file hardens this chain into a **one-click repeatable** test case:
  bring up stack (emqx + timescaledb:2.13.1 + redis + backend) -> wait for real health
  -> run mock_fleet in container (controlled --duration) -> count rows directly against DB -> assert.

Assertion and "no PK-conflict row loss"
--------------------------------------
1. `robot_telemetry` row count **>= 30000** (not `==`: network/scheduling jitter allows downward
   float; using `>=` is an explicit ISSUE #60 requirement).
2. **No PK-conflict row loss**: evidence from both sides is required, neither alone suffices --
   a) no `(time, robot_id)` duplicate groups exist in the DB (`GROUP BY ... HAVING count(*)>1`),
      and the PK unique constraint truly exists on the TimescaleDB side;
   b) the backend's self-reported row-loss triple: `rows_written == rows_submitted` and
      `rows_skipped == 0` and `rejected == 0`.
   `rows_skipped` is exactly the **silent row-loss** counter for `ON CONFLICT DO NOTHING`
   (`backend_ingestion/app/services/batch_writer.py:166-174`); looking only at "no error in logs"
   or only at "enough rows" would never catch it.
   (a) and (b) must both hold: querying only the DB proves "what landed has no duplicates" but not
   "everything that should land did land"; trusting only the backend's self-report proves nothing
   about the in-DB reality.

Why a real TimescaleDB is mandatory (mock stand-ins forbidden)
-------------------------------------------------------------
The `init.sql` features this test exercises (hypertable, continuous aggregate `robot_status_10s`,
`add_retention_policy` / `add_compression_policy`) **only exist in TimescaleDB**. Substituting
PostgreSQL or an in-memory stand-in decouples "e2e passes" from "production tables built correctly",
which is precisely what ISSUE #60 explicitly forbids.

When Docker is absent
--------------------
**skip and report clearly** (`pytest.skip` / exit code 3), never red. Decision order:
is the `docker` command present -> `docker info` (daemon reachable) -> can the image/stack come up.
Each layer gives "why skipped" and "what is needed to verify", avoiding masking an environment
problem as a code failure.

How to run
----------
    # with pytest
    python -m pytest tests/e2e/test_e2e_30k.py -v -s
    # without pytest (this file has its own main; exit code 0=pass 3=skip 1=fail)
    python tests/e2e/test_e2e_30k.py

Metric-naming anti-example probe (Order 32 / ISSUE #100)
--------------------------------------------------------
Besides the 30k landing, this file **also** verifies metric naming once on the real stack:
`/healthz` and `/metrics` are two endpoints exposed by the same process, so with the stack already
up, grabbing the real `/metrics` **raw bytes** to assert "new name present, old name gone" is
harder than spinning up a separate mock. `assert_metric_naming()`'s failure target is the **service**
(residual old name / missing new name), which is not an environment issue and, per the "do not fake
skip" rule, must go red -- see that function's comment. Why not a unit test:
`backend_ingestion/tests/test_observability.py` uses `metrics.render()` (**in-process, with its own
registry**) + `TestClient`, which cannot prove "the bytes emitted by the actually-running process" --
and `robot_monitor_*` is exactly the kind of thing that can resurrect by **registering one more
Collector at runtime**. The probe must watch that process, so it has to be inside the stack, against
the real port.

Environment variables (all have defaults; the defaults are ISSUE #60's acceptance criteria):
    E2E_DURATION=40        stress seconds (40s x 100 robots x 10Hz ~= 40,000 frames)
    E2E_ROBOTS=100         robot count
    E2E_MIN_ROWS=30000     row threshold
    E2E_KEEP=1             keep the stack after finishing (for investigation)
    E2E_UP_TIMEOUT=300     stack-up + health-wait timeout (seconds)
    E2E_COMPOSE=<path>     override compose file path
    E2E_METRICS_URL=<url>  /metrics endpoint (default backend compose-mapped 18000 port)
    E2E_METRICS_PREFIX=... only take metric rows with this prefix for the naming assertion (default robot_)

The stress run's own send-volume floor
---------------------------------------
The script computes a floor as `E2E_ROBOTS * 10 * E2E_DURATION * 0.5` and injects it as
`FLEET_MIN_SENT` into mock_fleet (`simulator/mock_fleet.py --min-sent`).
Why this must exist: `--duration` only guarantees the full duration is run, not the send volume, and
`docker compose run --rm` **does not** clear the default value compose set for the same variable --
so when the robot count is silently overridden, the stress run still exits 0, and the failure only
surfaces at the assertion stage (or is even passed by the threshold). The floor makes the stress
stage itself go red first, with a failure message pointing straight at the cause.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(os.environ.get(
    "E2E_COMPOSE", ROOT / "tests" / "e2e" / "docker-compose.e2e.yml"))

DURATION = int(os.environ.get("E2E_DURATION", "40"))
ROBOTS = int(os.environ.get("E2E_ROBOTS", "100"))
MIN_ROWS = int(os.environ.get("E2E_MIN_ROWS", "30000"))
UP_TIMEOUT = int(os.environ.get("E2E_UP_TIMEOUT", "300"))
KEEP = os.environ.get("E2E_KEEP") == "1"

# Host-side probe port for /healthz (compose maps 18000->8000).
# We hit the host port rather than `compose exec` to avoid depending on curl/wget inside the
# container -- the backend image is only python:3.11-slim and does not have them.
BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://127.0.0.1:18000")

# Metric-naming probe (Order 32): use the host-mapped port, for the same reason as BACKEND_URL
# (the backend image is only python:3.11-slim with no curl/wget inside; hitting from outside is easier).
METRICS_URL = os.environ.get("E2E_METRICS_URL", BACKEND_URL + "/metrics")
METRICS_PREFIX = os.environ.get("E2E_METRICS_PREFIX", "robot_")

PYTEST_SKIP_MARKER = "E2E-SKIP"

# R-13 / R-16: robot ID and insert-row count used by the cagg real-time backfill assertion
# (insert [now-30min, now] at 10Hz; R-16's `robot_status_10s` inserts [now-30s, now] per its 1min
# real-time-segment criterion -- see CAGG_PLAN in that file).
ROBOT_ID = os.environ.get("E2E_CAGG_ROBOT_ID", "humanoid-001")
BACKFILL_ROWS = int(os.environ.get("E2E_CAGG_ROWS", "1800"))

# Every subprocess must have an upper bound; no infinite waiting (ISSUE #60 §3 "reuse test_integration.sh's
# timeout-guard idea"). Image download/build can be slow, so up is generous; the rest are local ops, tens of
# seconds suffice.
UP_TIMEOUT_S = UP_TIMEOUT
HEALTH_TIMEOUT_S = 180
STRESS_TIMEOUT_S = DURATION + 120
# Send-volume floor: half of the theoretical value (robots x 10Hz x seconds). The half margin exists
# because early in stack-up the broker is not ready and there is reconnect idle-spin; a real **regression**
# (wrong broker, robot count overridden, publisher persistently erroring) drops it to a fraction, and the two
# are extremely distinguishable, so no need to be precise.
MIN_SENT = int(ROBOTS * 10 * DURATION * 0.5)
PSQL_TIMEOUT_S = 60


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


def compose(*args: str, timeout: int = PSQL_TIMEOUT_S, check: bool = True):
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args],
               timeout=timeout, check=check)


# ------------------------------------------------------------------ prerequisites

def require_docker() -> None:
    """skip if no Docker (with a clear reason), never red."""
    if shutil.which("docker") is None:
        raise E2ESkip(
            "docker command not found in PATH. This test needs a real Docker engine to bring up "
            "emqx + timescaledb:2.13.1-pg16 + redis, and **forbids** mock stand-ins "
            "(ISSUE #60 forbidden item). Need: run on a machine/runner with Docker installed."
        )
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "docker daemon not reachable (`docker info` non-zero)."
            f"Raw output: {(probe.stderr or probe.stdout).strip()[:400]}"
        )
    if not COMPOSE_FILE.is_file():
        raise E2ESkip(f"compose file does not exist: {COMPOSE_FILE}")
    # `docker compose` (plugin version) must be available: the legacy `docker-compose` (standalone
    # binary) uses a different command format than what the script calls, and mixing them would blow
    # up as a "code failure" only at stack-up time. This check **cannot** go inside the try below --
    # that one catches docker daemon not ready (E2ESkip), whereas this is a missing dependency (docker
    # present but no compose plugin), which must skip explicitly with a reason.
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        raise E2ESkip(
            "`docker compose` (plugin version) unavailable, `docker compose version` non-zero. "
            "This test's entire stack-up/execution/evidence path uses compose. Raw output: "
            f"{(probe.stderr or probe.stdout).strip()[:400]}"
        )


# ------------------------------------------------------------------ probes

def backend_health() -> dict | None:
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/healthz", timeout=3) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def wait_backend_db_up(timeout: int = HEALTH_TIMEOUT_S) -> dict:
    """Wait until /healthz reports db=up.

    Why not wait for "container running": `/healthz` is a **liveness-probe** semantic and still
    returns 200 with status=degraded when the dependency is unreachable
    (backend_ingestion/app/main.py:85-118). If we started running at that point, the simulator's
    first batch would pile up in the queue and only land once the DB is up -- row counts look fine
    but the timing is already scrambled. So we must wait for the **semantic** db=up, not the port.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = backend_health()
        if last and last.get("db") == "up":
            return last
        time.sleep(2)
    raise AssertionError(
        f"backend /healthz did not reach db=up within {timeout}s (last snapshot: {last})"
    )


def psql(sql: str) -> str:
    """Execute SQL inside the timescaledb container, return a single value (whitespace stripped)."""
    proc = compose("exec", "-T", "timescaledb", "psql", "-U", "postgres",
                   "-d", "robot_monitor", "-tAc", sql)
    return proc.stdout.strip()


# ------------------------------------------------------------------ test body

def run_e2e() -> dict:
    """Execute the full chain and return the evidence result (shared by assertions and reporting)."""
    evidence: dict = {}

    print(f"[e2e] compose = {COMPOSE_FILE}", flush=True)
    print(f"[e2e] criteria: {ROBOTS} robots x 10Hz x {DURATION}s, threshold >= {MIN_ROWS} rows", flush=True)

    # 1) Bring up the stack (build + pull images). Do not bring up mock_fleet with up: it lives under
    #    the stress profile, and the stress run must come after "backend can land rows", otherwise we
    #    are testing a system that is not ready yet.
    up_started = time.monotonic()
    compose("up", "-d", "--build", timeout=UP_TIMEOUT_S)
    evidence["up_seconds"] = round(time.monotonic() - up_started, 1)
    print(f"[e2e] stack up ({evidence['up_seconds']}s)", flush=True)

    # 2) Wait for the backend to confirm DB is reachable
    health = wait_backend_db_up()
    print(f"[e2e] /healthz = {json.dumps(health, ensure_ascii=False)}", flush=True)
    evidence["health_before"] = health

    # 3) Pre-stress baseline (when reusing a stack the baseline may be non-zero; this test defaults to
    #    down -v each run, so it is usually 0)
    before = int(psql("SELECT count(*) FROM robot_telemetry;") or 0)
    evidence["rows_before"] = before
    print(f"[e2e] pre-stress robot_telemetry = {before}", flush=True)

    # 4) Run a controlled-duration stress test inside the container. Use `compose run` rather than `up`:
    #    run is foreground-blocking and its exit code is the stress result, giving a natural sync point
    #    instead of polling container state to guess "did it finish".
    print(f"[e2e] stress send-volume floor = {MIN_SENT} rows (half of theoretical {ROBOTS * 10 * DURATION})",
          flush=True)
    stress_started = time.monotonic()
    compose(
        "run", "--rm", "-T",
        "-e", f"FLEET_DURATION={DURATION}",
        "-e", f"FLEET_ROBOTS={ROBOTS}",
        "-e", f"FLEET_MIN_SENT={MIN_SENT}",
        "mock_fleet",
        timeout=STRESS_TIMEOUT_S,
    )
    evidence["stress_seconds"] = round(time.monotonic() - stress_started, 1)
    print(f"[e2e] stress finished ({evidence['stress_seconds']}s)", flush=True)

    # 5) Wait for the backend to flush in-flight batches.
    #    The dual trigger is "every 500 rows **or** every 1s", and the last small batch waits up to 1s
    #    to flush (backend_ingestion/app/services/batch_writer.py). We must not bare-sleep some
    #    made-up value here; we wait until the queue is empty AND the count stops growing, otherwise
    #    we get a false failure of "slightly fewer rows".
    health = wait_writer_drained()
    evidence["health_after"] = health
    print(f"[e2e] /healthz (settled) = {json.dumps(health, ensure_ascii=False)}", flush=True)

    # 6) In-DB evidence (real SQL counts, not logs)
    after = int(psql("SELECT count(*) FROM robot_telemetry;") or 0)
    dup_groups = int(psql(
        "SELECT count(*) FROM ("
        "  SELECT time, robot_id FROM robot_telemetry"
        "  GROUP BY time, robot_id HAVING count(*) > 1"
        ") t;"
    ) or 0)
    pk_defined = psql(
        "SELECT coalesce(string_agg(a.attname, ',' ORDER BY array_position(c.conkey, a.attnum)), '')"
        " FROM pg_constraint c"
        " JOIN pg_class r ON r.oid = c.conrelid"
        " JOIN pg_attribute a ON a.attrelid = r.oid AND a.attnum = ANY(c.conkey)"
        " WHERE r.relname = 'robot_telemetry' AND c.contype = 'p';"
    )
    hypertable = psql(
        "SELECT count(*) FROM timescaledb_information.hypertables"
        " WHERE hypertable_name = 'robot_telemetry';"
    )
    db_version = psql("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")

    evidence.update({
        "rows_after": after,
        "rows_delta": after - before,
        "dup_pk_groups": dup_groups,
        "pk_columns": pk_defined,
        "is_hypertable": hypertable,
        "timescaledb_version": db_version,
    })
    print(f"[e2e] in-DB evidence: after={after} delta={after - before} "
          f"dup_pk_groups={dup_groups} pk=({pk_defined}) "
          f"hypertable={hypertable} tsdb={db_version}", flush=True)

    # 7) Metric-naming anti-example probe (Order 32): the stack is alive, so grab the real /metrics
    #    raw bytes while we're here. Done after the landing evidence: ensure the main chain works
    #    before verifying naming -- otherwise "the service never came up" would be misreported as
    #    "naming problem".
    assert_metric_naming(METRICS_URL, METRICS_PREFIX)

    # 8) cagg real-time aggregation backfill (stack alive, reuse real TimescaleDB):
    #    R-13's two `/history` data sources + R-16's `robot_status_10s` (dashboard latest state).
    #    Why fold it in here rather than bring up a separate stack: CI's e2e stage only runs this file,
    #    and the stack-up cost is already paid; a separate stack just to run a few count assertions is
    #    wasteful (R-13 doc §7 explicitly allows "fold into the existing e2e", and R-16 reuses the same
    #    integration). The assertion body lives in test_cagg_realtime_backfill.py; this file passes its
    #    **own** psql in, sharing the same container connection (same `-U postgres -d robot_monitor`)
    #    with the 30k landing. The moment someone deletes or flips back to true the
    #    `SET (materialized_only = false)` in init.sql §5.1c (R-13) or §5.1d (R-16), this goes red
    #    immediately -- that is the regression alarm these two requirements hang on.
    from test_cagg_realtime_backfill import assert_cagg_realtime_backfill  # noqa: PLC0415

    evidence["cagg_realtime_backfill"] = assert_cagg_realtime_backfill(
        psql_fn=psql, robot_id=ROBOT_ID, rows=BACKFILL_ROWS)
    print(f"[e2e] cagg real-time backfill (R-13 /history x2 + R-16 dashboard latest state): "
          f"{evidence['cagg_realtime_backfill']}", flush=True)

    return evidence


def wait_writer_drained(timeout: int = 60) -> dict:
    """Wait until the BatchWriter queue drains and counts stabilize.

    Judge "stable" rather than "queue is 0": the queue being momentarily 0 could also be right
    between two batches before the next one comes in. Conclude settled only when `rows_submitted`
    is unchanged across two consecutive 1s intervals.
    """
    deadline = time.monotonic() + timeout
    prev = None
    while time.monotonic() < deadline:
        h = backend_health()
        if h is not None:
            stable = (prev is not None
                      and h.get("rows_submitted") == prev.get("rows_submitted")
                      and h.get("queue_depth") == 0)
            if stable:
                return h
            prev = h
        time.sleep(1)
    last = backend_health()
    raise AssertionError(
        f"BatchWriter did not drain/stabilize within {timeout}s (last /healthz: {last}). "
        "A persistently backed-up queue means the landing rate cannot keep up with 1000 msg/s; "
        "check /metrics."
    )


def teardown() -> None:
    if KEEP:
        print("[e2e] E2E_KEEP=1, keeping the stack (manual cleanup: "
              f"docker compose -f {COMPOSE_FILE} down -v)", flush=True)
        return
    # down -v: delete volumes too. Each e2e needs a "brand-new DB + init.sql re-run";
    # keeping the volume would mix last round's data into this round's counts (baseline non-zero
    # would skew the threshold judgment).
    compose("down", "-v", "--remove-orphans", check=False, timeout=120)


# ------------------------------------------------------------------ assertions

def assert_e2e(ev: dict) -> None:
    # 0) Precondition: confirm we are connected to a **real TimescaleDB**, not a PG stand-in.
    #    Without this, the later assertions would still go green when "mistakenly connected to a plain PG".
    assert ev["is_hypertable"] == "1", (
        "robot_telemetry is not a hypertable -- meaning we are not connected to a TimescaleDB that "
        f"ran init.sql (is_hypertable={ev['is_hypertable']!r})."
    )
    assert ev["pk_columns"] == "time,robot_id", (
        "robot_telemetry primary key shape is wrong: expected (time, robot_id), "
        f"got ({ev['pk_columns']!r}). The PK is the anchor for the 'no conflict row loss' judgment, "
        "so it must be anchored correctly first."
    )
    assert ev["timescaledb_version"], "timescaledb not found in pg_extension; DB is not a TimescaleDB instance"

    # 1) Row-count threshold (ISSUE #60: >= 30000, `==` forbidden)
    assert ev["rows_delta"] >= MIN_ROWS, (
        f"robot_telemetry added {ev['rows_delta']} rows < threshold {MIN_ROWS}"
        f"(stress {DURATION}s x {ROBOTS} robots x 10Hz expected ~= {ROBOTS * 10 * DURATION})."
        f"baseline {ev['rows_before']} -> end {ev['rows_after']}."
    )

    # 2) No PK-conflict row loss -- in-DB fact side
    assert ev["dup_pk_groups"] == 0, (
        f"DB contains {ev['dup_pk_groups']} duplicate (time, robot_id) groups, "
        "meaning the write path's idempotency is broken (ON CONFLICT not in effect or PK not built)."
    )

    # 3) No PK-conflict row loss -- backend self-report side (rows_skipped is the only silent row-loss counter)
    h: dict = ev["health_after"]
    assert h.get("rejected", 1) == 0, (
        f"rejected={h.get('rejected')}: degradation-chain L3 judged bad rows discarded, a true data loss."
    )
    assert h.get("rows_skipped", 1) == 0, (
        f"rows_skipped={h.get('rows_skipped')}: rows silently dropped by ON CONFLICT DO NOTHING is non-zero, "
        "i.e. 'not all received data landed'. This is exactly the row-loss evidence defined by ISSUE #19 G2."
    )
    assert h.get("rows_written") == h.get("rows_submitted"), (
        f"rows_written={h.get('rows_written')} != rows_submitted={h.get('rows_submitted')}: "
        "submitted and landed counts differ, row loss exists."
    )
    assert h.get("db") == "up", f"final /healthz db={h.get('db')}, DB offline, data incomplete"

    # 4) Cross-check: in-DB added rows should not be **greater** than the backend's self-reported
    #    landed rows by much. The two are not fully equivalent (when reusing a stack, the baseline
    #    includes last round's data; the backend count is this process's cumulative), so we only do a
    #    directional check of "DB did not conjure extra rows", avoiding a fragile assertion.
    assert ev["rows_delta"] <= h.get("rows_written", 0) + 10, (
        f"in-DB added {ev['rows_delta']} rows > backend self-reported landed {h.get('rows_written')} "
        f"rows (+10 tolerance), meaning there is a write path bypassing the backend count, or the "
        f"baseline statistic is wrong."
    )


# ------------------------------------------------------------------ metric-naming anti-example probe (Order 32)

# The 6 old names that **were once registered as metrics** in Order 32 T1's pre-change `grep -rn
# robot_monitor_` list. They are this probe's failure surface: any one appearing in the real
# /metrics makes it red.
LEGACY_MONITOR_METRICS = (
    "robot_monitor_batch_queue_size",
    "robot_monitor_batch_flush_total",
    "robot_monitor_batch_rows_total",
    "robot_monitor_mqtt_messages_total",
    "robot_monitor_ws_connections",
    "robot_monitor_history_latency_seconds",
)

# Canonical names that must exist after the change (Order 32 T2's two genuinely new ones + the 4 that
# deduplicated and inherited the same-source counts).
#
# Reason for splitting into two groups (Order 34 empirical): prometheus_client, for a **Counter with
# labels**, exports **no sample line at all** (not even a 0-value line) until a particular label
# combination **has been observed** -- its time series is "born on first `.labels(...).inc()`".
# Evidence: PR #111's first CI (build cnb-dr0-1k4pbtmr7-002) ran the naming probe on a stack that had
# just finished `up -d` with **zero telemetry** and reported `missing canonical name
# ['robot_backend_batch_flushes_total']` -- yet that metric is clearly registered in
# `backend_ingestion/app/metrics.py:77` (`labelnames=("reason",)`); it is just that no
# `{reason=count|timeout}` flush batch had happened yet that round.
# This is a **probe-criterion factual error** (treating "no series" as "metric does not exist"), not a
# service defect. Hence the split into two classes: unlabeled ones must always be present; labeled ones
# are only required after "an event has occurred" -- the 30k landing path (step 7 of run_e2e() in this
# file) runs **after** the stress test, by which time flushes have necessarily happened, so its
# requirement is **not relaxed**; only the zero-traffic standalone `--metric-naming-check` in CI may
# allow their absence (and the caller must explicitly declare allow_unemitted=True; default relaxation
# is forbidden).
REQUIRED_BACKEND_METRICS = (
    "robot_backend_queue_depth",
    "robot_backend_telemetry_written_total",
    "robot_backend_mqtt_received_total",
    "robot_backend_ws_connections",
    "robot_backend_history_latency_seconds",
)

# Labeled Counter: only exported when an event occurs. See above; not in the "always present" class.
LABELED_BACKEND_METRICS = (
    "robot_backend_batch_flushes_total",
)


def metric_lines(metric_name_prefix: str, text: str) -> list[tuple[str, str]]:
    """Extract metric lines from /metrics text.

    Why this layer is necessary: `metrics.py`'s header comment **intentionally keeps** the old-name
    migration record (writing down "old name deprecated" is more useful than deleting it). A direct
    `in` over the whole text would misjudge the comment as a violating metric, creating a "permanently
    red" fake gate -- the worst fate of such a gate is someone adding `|| true` to disable it. Here we
    only recognize metric lines: Prometheus exposition metric lines look like `name{suffix}?[ ][value]`,
    while comment lines start with `#` (incl. `# HELP` / `# TYPE`).
    """
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        line_name = line.split("{", 1)[0].split(" ", 1)[0]
        if line_name.startswith(metric_name_prefix):
            cells = line.split()
            out.append({
                "name": line_name,
                "samples": [c.strip(",") for c in cells[1:]] or [""],
                "raw": line.strip(),
            })
    return out


def assert_metric_naming(url: str, prefix: str, allow_unemitted: bool = False) -> None:
    """Assert against the real service: canonical names present, old names gone.

    The two failure modes of fetching must be reported separately:
      - HTTP unreachable (the probe never ran) -> skip and explain, do not fake a pass;
      - HTTP reached but content violates the criterion (old name still there / new name missing) -> **red**.
    The latter is the only reason this probe exists: it is an **anti-example gate** and must not degrade
    into a skip when the assertion fails just because "it was only added along the way".

    allow_unemitted: only for the **zero-traffic** standalone probe call (the kind CI runs right after
    `up -d`). When True, the absence of labeled Counters (LABELED_BACKEND_METRICS) is recorded as info
    without going red -- because they are "exported only on event", so absence under zero traffic is
    **correct behavior**, not a regression. The 30k landing path (called after the stress test) must pass
    False (the default), requiring the full set to be present.
    """
    text = http_get(url)
    if text is None:
        _pytest_skip(
            f"metric-naming probe cannot reach {url} (backend:8000's /metrics). "
            "If the service did not come up, wait_backend_db_up already failed first; this is a probe "
            "misconfiguration."
        )
        return

    shown = metric_lines(prefix, text)
    # The hit criterion must be **per family**, not per literal name: in Prometheus exposition
    #   - Counter / Gauge / Summary have bare sample lines like `robot_backend_x <v>` starting with the name;
    #   - **Histogram has none** -- it only has `robot_backend_x_bucket{le=...}` / `_count` / `_sum`.
    # The first version wrote `name == "robot_backend_history_latency_seconds"` and could never hit,
    # creating a fake gate that "even offline positive cases cannot pass" (caught by this file's extra
    # empirical testing). So the criterion became "the name itself, or name + `_` followed by a subdivided suffix".
    names_here = {ln["name"] for ln in shown}

    def _hits(metric: str) -> list[dict]:
        return [ln for ln in shown if ln["name"] == metric
                or ln["name"].startswith(metric + "_")]

    leaked = [m for m in LEGACY_MONITOR_METRICS if _hits(m)]
    assert not leaked, (
        f"real /metrics still has legacy-prefix metrics {leaked} (Order 32 acceptance: old names must vanish)."
        f"Actual raw lines: {[ln['raw'] for m in leaked for ln in _hits(m)][:5]}"
    )
    missing = [m for m in REQUIRED_BACKEND_METRICS if not _hits(m)]
    # Labeled Counters are only exported after "an event has occurred"; zero-traffic probes allow their
    # absence, but they must be **printed** (silently letting them pass vs. explicitly recording are two
    # different things; this ticket repeatedly stresses that boundary).
    unemitted = [m for m in LABELED_BACKEND_METRICS if not _hits(m)]
    if unemitted and not allow_unemitted:
        missing = missing + unemitted
    if unemitted and allow_unemitted:
        print(f"[e2e] metric-naming probe: labeled metrics not yet exported (zero traffic this round, expected): {unemitted}",
              flush=True)
    assert not missing, (
        f"real /metrics missing canonical names {missing} (Order 32 acceptance: new names must be present)."
        f"Under this prefix, observed {len(names_here)} metric names, sample: {sorted(names_here)[:8]}"
    )
    total = len(REQUIRED_BACKEND_METRICS) + len(LABELED_BACKEND_METRICS) - len(unemitted)
    print(f"[e2e] metric-naming probe passed: 0 legacy-prefix hits, {total} canonical names present",
          flush=True)


def http_get(url: str, timeout: int = 5) -> str | None:
    """Fetch URL text; return None on failure (caller decides skip or red)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# ------------------------------------------------------------------ report

def render_report(ev: dict) -> str:
    """Produce an evidence report that can be pasted directly into an Issue/PR (SQL counts + command criteria)."""
    h = ev["health_after"]
    lines = [
        "## e2e 30k landing evidence (real TimescaleDB)",
        "",
        f"- Stack: `tests/e2e/docker-compose.e2e.yml` (emqx:5.6 + **timescaledb {ev['timescaledb_version']}** + redis:7.2 + backend + mock_fleet)",
        f"- Stack-up time: {ev['up_seconds']}s; stress: {ev['stress_seconds']}s ({DURATION}s controlled duration)",
        f"- `SQL> SELECT count(*) FROM robot_telemetry;` -> pre-stress {ev['rows_before']} / post {ev['rows_after']}"
        f" (**added {ev['rows_delta']}**, threshold >= {MIN_ROWS})",
        f"- `SQL> SELECT count(*) FROM (... GROUP BY time, robot_id HAVING count(*)>1) t;` -> **{ev['dup_pk_groups']}** (PK conflict groups)",
        f"- `SQL> {('robot_telemetry PK = (' + ev['pk_columns'] + ')')}`; hypertable = {ev['is_hypertable']}",
        f"- backend `/healthz` row-loss triple: rows_submitted={h.get('rows_submitted')} / "
        f"rows_written={h.get('rows_written')} / rows_skipped={h.get('rows_skipped')}"
        f"; rejected={h.get('rejected')}; mqtt_dropped={h.get('mqtt_dropped')}",
        "",
        "Conclusion: **PASS** -- row count meets threshold and no PK-conflict row loss.",
    ]
    return "\n".join(lines)


def _pytest_skip(reason: str):
    """Skip under both pytest and bare-run contexts.

    Implementation note (learned empirically): cannot judge the context by `import pytest` alone --
    pytest **is installed in this environment**, so that import succeeds even when bare-running, and
    then the `main()` path that wants "print reason + exit code 3" would become `pytest.skip()`
    throwing a `Skipped` exception straight onto the traceback (encountered when adding
    `--metric-naming-check` to this file). The criterion switches to `under_pytest()` (pytest injects
    itself into sys.modules and runs tests in a module that is **not __main__**).
    """
    if under_pytest():
        import pytest  # noqa: PLC0415

        pytest.skip(reason)
    print(f"{PYTEST_SKIP_MARKER}: {reason}", flush=True)
    sys.exit(3)


def under_pytest() -> bool:
    """Whether the current driver is pytest (rather than a bare `python tests/e2e/test_e2e_30k.py` run)."""
    return "pytest" in sys.modules and __name__ != "__main__"


def test_e2e_30k_landing() -> None:
    try:
        require_docker()
    except E2ESkip as exc:
        _pytest_skip(str(exc))

    try:
        ev = run_e2e()
        print("\n" + render_report(ev) + "\n", flush=True)
        assert_e2e(ev)
    finally:
        teardown()


def dump_failure_logs() -> None:
    """Chain-failure evidence: grab the tail logs of the timescaledb / backend key containers.

    Only called on the failure path; `--tail` is capped to avoid flooding the screen. `compose logs`
    also works on **already-exited** containers (reads the stdout buffer), so a DB init crash midway
    (e.g. CAGG cannot be built) is still catchable. Lesson from 2026-10-11: relying only on
    "/healthz not db=up within 180s (snapshot None)" cannot distinguish ①init.sql table creation
    failed ②backend container crashed on startup ③backend started but cannot connect to DB -- the three
    fixes are completely different, so the logs must speak for themselves.
    """
    for svc in ("timescaledb", "backend"):
        try:
            proc = compose("logs", "--tail=80", svc, timeout=30, check=False)
            print(f"\n===== compose logs {svc} (tail 80) =====", flush=True)
            print((proc.stdout or "")[-8000:] or "(no output)", flush=True)
        except Exception as exc:  # evidence collection itself must never alter the exit code
            print(f"(evidence collection failed {svc}: {exc})", flush=True)


def main() -> int:
    """Entry point without pytest: 0=pass 1=assertion-fail 3=env-not-ready (skip).

    There is also `--metric-naming-check`: does **not** require docker; it just runs Order 32's
    naming anti-example assertion once against an already-running stack. In CI the stack is brought
    up via `docker compose up -d` (without the stress profile), and this switch lets you hang the
    naming gate into the existing pipeline without bringing up a stack again inside pytest.
    """
    if "--metric-naming-check" in sys.argv:
        # Env not ready -> 3 (skip; CI must convert to 0 and print the reason); assertion failed -> 1 (**must be red**).
        if not COMPOSE_FILE.is_file():
            print(f"{PYTEST_SKIP_MARKER}: compose file does not exist: {COMPOSE_FILE}", flush=True)
            return 3
        try:
            # allow_unemitted=True: this switch runs on a **zero-traffic** stack (CI after `up -d`, before
            # stress), where labeled Counters not yet born is correct behavior. Residual old names / missing
            # unlabeled names still go red. The 30k landing path (run_e2e step 7, after stress) uses the
            # default False, requiring the full set to be present.
            assert_metric_naming(METRICS_URL, METRICS_PREFIX, allow_unemitted=True)
        except AssertionError as exc:
            print(f"[e2e] metric-naming gate not passed: {exc}", flush=True)
            return 1
        except E2ESkip as exc:  # assert_metric_naming internally goes through _pytest_skip; catch here
            print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
            return 3
        return 0

    try:
        require_docker()
    except E2ESkip as exc:
        print(f"{PYTEST_SKIP_MARKER}: {exc}", flush=True)
        return 3

    try:
        ev = run_e2e()
    except AssertionError as exc:
        print(f"[e2e] chain failed: {exc}", flush=True)
        dump_failure_logs()
        teardown()
        return 1

    try:
        assert_e2e(ev)
    except AssertionError as exc:
        print(f"[e2e] assertion failed: {exc}", flush=True)
        print("\nEvidence snapshot:\n" + render_report(ev), flush=True)
        teardown()
        return 1

    print("\n" + render_report(ev), flush=True)
    teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
