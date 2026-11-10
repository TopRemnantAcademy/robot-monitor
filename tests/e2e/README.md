# e2e: End-to-end 30,000-row landing verification (ISSUE #60)

Pin the chain "100 units × 10Hz → MQTT → backend batch landing → real TimescaleDB ≥ 30,000 rows with no PK-conflict row loss" into a single repeatable command. Previously this chain was only verified at the **unit-test scope**; both `STATUS.md` §9 and `docs/requirements/R-09-database-integration.md` §6 still carried the note "still unverified".

## Files

| File | Role |
| :--- | :--- |
| `docker-compose.e2e.yml` | One-shot stack: `emqx:5.6` + `timescaledb:2.13.1-pg16` + `redis:7.2` + `backend` + `mock_fleet` (`stress` profile) |
| `Dockerfile.mock_fleet` | Simulator image (`simulator/` originally only had bare-run, no Dockerfile) |
| `test_e2e_30k.py` | Driver + assertions + forensic report; without Docker, **skip (exit code 3)**, never red |

## How to run in CI

`.cnb.yml`'s `e2e-30k-landing` pipeline (PR / push main / daily crontab — three triggers) attaches `services: [docker]` and **explicitly pins `docker.image: python:3.11-slim`**.

Why the image must be pinned: on the first real run, `image` was not written; the default image `cnbcool/default-build-env` **ships no python3**, so after the docker probe had already passed, `sh: line 21: python3: command not found` (return code 127) appeared — the job went red but had nothing to do with the code. The script also does two-layer probing (docker → python); if either layer is missing it prints [E2E SKIP]+ reason + fix and exits 0, never masking an environment issue as a regression.

### The three triggers must share one declaration (Order 34 attribution)

After Order 32 lengthened the e2e stage, the push / pull_request blocks were updated, **but the `"crontab: 0 3 * * *"` block was not changed**: it only wrote `services/env/stages` and **omitted `docker.image`**. So the same stages ran under the default image on the crontab trigger, the `python` probe layer hit immediately → printed [E2E SKIP] → `exit 0` → the whole build stayed green, but **not a single row was landed**. The two builds of the same sha (`68b7b8b`) are the control group:

- push `cnb-152-1k4ohjp3k-002` prepare: `docker pull python:3.11-slim`, e2e stage prints `python available: Python 3.11.17`
- crontab `cnb-st7-1k4onp4ok-001` prepare: `docker pull cnbcool/default-build-env`, e2e stage prints [E2E SKIP]

Current state: `docker.image` is extracted into the YAML anchor `&e2e-image`, and the three `docker: *e2e-image` reference the same value — consistency is guaranteed at the YAML layer, no longer relying on a human remembering to change it three times.

### SKIP must print a banner (Order 34 T2)

Any exit path that "did not really run" first calls `skip_banner`, printing a fixed string:

```
================================================================
[E2E SKIPPED -- this build did not actually run the 30k landing]
Reason: <specific reason>
This build's e2e is green but **produced no landing numbers**, and must not be treated as evidence for ISSUE #60.
================================================================
```

Previously SKIP only printed a single line `[E2E SKIP]...`, lost among hundreds of log lines, indistinguishable from noise; on the CI list you only saw "green".

### The naming probe originally used `curl`, unreachable in `python:3.11-slim` (Order 34 T2)

`python:3.11-slim` is Debian slim and **ships no curl/wget**. But the probe written in Order 32 used `curl -sf http://127.0.0.1:18000/healthz` / `/metrics` — so regardless of whether the stack came up, it judged "unreachable", **and this gate never actually ran from the day it was attached** (`cnb-sfb-1k4oh9va6-002`, `cnb-152-1k4ohjp3k-002`'s e2e stage logs both show "[E2E SKIP]Order 32 naming probe: /metrics unreachable within 180s"). It is now changed to call `$PY -c "import urllib.request; ..."` (this job's own runtime), introducing no new dependency.

### The naming probe's two classes of judgment: no-label / with-label (Order 34)

After fixing `curl`, the probe turned red on its first real run: `/metrics` was missing `robot_backend_batch_flushes_total`. **This is not a naming regression** — that metric is registered fine at `backend_ingestion/app/metrics.py:77`; what's missing is the **sample line**: prometheus_client lazily registers Counters with labels, and the series is only born on the first `labels(reason=...).inc()`. And this stage brings up the stack with `up -d` and probes immediately, without starting `mock_fleet`, so the backend never flushed a batch.

Both fixes are valid, with different boundaries:

- **Probe side (adopted in this order)**: split the judgment into `REQUIRED_BACKEND_METRICS` (no-label, should always be present) and `LABELED_BACKEND_METRICS` (only exported when an event occurs). For the zero-traffic standalone `--metric-naming-check`, pass `allow_unemitted=True`: absence is **printed but not judged red**; the 30k landing path (after the load test) keeps the default `False`, requiring everything in place — **no discount** on its requirements.
- **Service side (the other fix, not adopted in this order)**: before exposing `/metrics`, `inc(0)` pre-warm each constant enum label, so the labeled Counter has a 0-value line "from the first scrape". This also eliminates the "metric doesn't exist" vs "metric is always 0" ambiguity on the panel — **a genuine observability improvement** — but it modifies `backend_ingestion/app/metrics.py`, which falls outside this order's file whitelist (whitelist: `.cnb.yml` / `tests/e2e/**` / `STATUS.md` / `docs/**`), so it doesn't belong in this PR; it should be a separate order (follow-up to Order 32).

The two don't conflict: after the service-side warm-up, the `allow_unemitted` branch won't be triggered. This order picks the probe side, purely due to the **whitelist constraint**.

### The shell aborts the whole script after a non-zero (Order 34 T2)

Measured (`cnb-dr0-1k4pbtmr7-002`): after the naming probe returned 1, the script **did not** continue to run 30k, but directly `Finished, code: 1, duration: 1m 1s`. That is, this stage's shell has `set -e` semantics, not something `set -uo pipefail` can turn off. Hence every command that might be non-zero swallows its exit code locally (`cmd || rc=$?`), consolidating the judgment to an explicit `exit` at the end of the script.

## How to run

```bash
# pytest
python -m pytest tests/e2e/test_e2e_30k.py -v -s

# or bare-run (0=pass 1=assertion failure 3=environment not ready)
python tests/e2e/test_e2e_30k.py
```

Manual step-by-step (for troubleshooting):

```bash
docker compose -f tests/e2e/docker-compose.e2e.yml up -d --build
# wait for /healthz db=up
FLEET_DURATION=40 docker compose -f tests/e2e/docker-compose.e2e.yml \
  --profile stress run --rm mock_fleet
docker compose -f tests/e2e/docker-compose.e2e.yml exec -T timescaledb \
  psql -U postgres -d robot_monitor -tAc "SELECT count(*) FROM robot_telemetry;"
docker compose -f tests/e2e/docker-compose.e2e.yml down -v
```

## Environment variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `E2E_DURATION` | `40` | Load duration in seconds (40s × 100 units × 10Hz ≈ 40,000 frames) |
| `E2E_ROBOTS` | `100` | Number of units |
| `E2E_MIN_ROWS` | `30000` | Row threshold (ISSUE #60 scope) |
| `E2E_UP_TIMEOUT` | `300` | Timeout for bringing up the stack + pulling/building images (seconds) |
| `E2E_KEEP` | — | `1` keeps the stack after finishing, for manual troubleshooting |
| `E2E_COMPOSE` | `tests/e2e/docker-compose.e2e.yml` | Override the compose file path |

`E2E_ROBOTS` / `E2E_DURATION` are converted to `FLEET_MIN_SENT = units × 10 × seconds × 0.5` and injected into `mock_fleet` (see next item).

## Assertion scope (why written this way)

1. **Use `>=` for row count, not `==`**: network/scheduling jitter allows a downward float (per ISSUE #60).
2. **"No PK-conflict row loss" requires evidence on both sides, neither alone suffices**:
   - In-DB: `GROUP BY time, robot_id HAVING count(*)>1` must be 0, and PK shape is `(time, robot_id)`;
   - Backend: `rows_written == rows_submitted`, `rows_skipped == 0`, `rejected == 0`. `rows_skipped` is the only counter for **silent row loss** from `ON CONFLICT DO NOTHING` (`backend_ingestion/app/services/batch_writer.py:166-174`).
   Querying the DB alone can't catch "should-have-landed-but-didn't"; trusting self-reported numbers alone can't establish "in-DB facts".
3. **Must first be a real TimescaleDB**: the assertion first checks `is_hypertable=1` and the `timescaledb` extension version, to prevent a false-green when accidentally connected to a plain PostgreSQL. Mock substitutes are explicitly forbidden by ISSUE #60.
4. **Wait for `/healthz` with `db=up` before starting**: `/healthz` is a liveness-probe semantic; it still returns 200 when dependencies are unreachable, so judging only by port would start early; at the end, wait until `rows_submitted` is unchanged twice in a row to consider landing settled (queue drained).
5. **The load stage carries its own send lower bound**: `mock_fleet --min-sent` (injected by the script as `units×10×seconds×0.5`). With only `--duration`, the simulator **exits 0 when it runs its full time**, and won't complain if it couldn't send: `docker compose run --rm` doesn't clear the default value set for the same variable in compose, so a silently-overridden unit count, a wrong broker, or a continuously-erroring publish side can all coast to the assertion stage or even slip past the threshold. Adding a lower bound makes failures happen **at the point closest to the cause**, with the log naming the reason directly.

## Order 32: metric-naming counter-example probe (`--metric-naming-check`)

Besides the landing assertions, `test_e2e_30k.py` also asserts metric naming against the **raw bytes of the real `/metrics`**:

```bash
# when the stack is running (with backend's 18000→8000 mapping):
python tests/e2e/test_e2e_30k.py --metric-naming-check
# 0=pass 1=naming out of scope (red) 3=environment not ready (skip)
```

It asserts two things: 0 hits for the 6 `robot_monitor_*` legacy names, and all 6 `robot_backend_*` canonical names present. Why it must be a runtime probe rather than a unit test: `backend_ingestion/tests/test_observability.py` runs `metrics.render()` (in-process registry) + `TestClient`, **which cannot prove "the bytes the actually-running process emits"** — and the legacy prefix is exactly the kind of thing that "revives if you register one more Collector". The probe watches the process, so it must be in the stack, against the real port.

Details (all learned from real runs):

- The hit criterion is **per-metric-family**, not per-literal-name: a Histogram has no `name <value>` bare sample line, only `name_bucket{le=}` / `_count` / `_sum`. The first version matched literals by equality and couldn't even pass the positive case — that would have created a "permanently red" fake gate.
- Only recognize **metric lines** (skip `#` comments): `metrics.py`'s header comment deliberately keeps the legacy-name migration records; a full-text `in` assertion would misjudge the migration records as violations.
- The probe does its own negative self-check (see PR description): after injecting `robot_monitor_ws_connections`, it must go red.

In CI it is attached at the **very front** of the `e2e-30k-landing` stage, runs its own `up -d` (without the stress profile) to get a live backend, asserts, then `down`. HTTP unreachable → `[SKIP]` and exit 0; assertion failure → exit code 1 → **stage goes red** (a gate that swallows a failure is worse than no gate).

## Real run records

### 2026-10-13 — First real landing (Order 34 T3)

Build: PR **#111**'s pull_request build **`cnb-vro-1k4pcqp07`** (pipeline `-002`, e2e stage **160.8s**, not SKIP). Runtime `python:3.11-slim` (`python available: Python 3.11.17`) + `services: [docker]`.

```
== e2e 30k landing verification (ISSUE #60) ==
docker available: Docker version 29.6.2, build dfc4efb
python available: Python 3.11.17
== Order 32: metric-naming counter-example probe ==
[e2e] metric naming probe: labeled metric not yet exported (this round zero traffic, expected): ['robot_backend_batch_flushes_total']
[e2e] metric naming probe passed: legacy-name prefix 0 hits, canonical name 5 present
Order 32 naming gate passed: /metrics legacy-name 0 hits, robot_backend_* canonical names all present.
== 30k landing main chain (this job's hard-threshold output) ==
[e2e] compose = /workspace/tests/e2e/docker-compose.e2e.yml
[e2e] scope: 100 units × 10Hz × 40s, threshold >= 30000 rows
[e2e] stack up (9.1s)
[e2e] /healthz = {"status": "ok", "db": "up", ..., "rows_submitted": 0, "rows_written": 0, "rows_skipped": 0}
[e2e] pre-load robot_telemetry = 0
[e2e] load send lower bound = 20000 (half of theoretical 40000)
[e2e] load ended (52.5s)
[e2e] /healthz (after settle) = {"status": "ok", "db": "up", "written": 39864, "flushes": 80,
      "retries": 0, "rejected": 0, "queue_depth": 0, "mqtt_received": 39864, "mqtt_dropped": 0,
      "rows_submitted": 39864, "rows_written": 39864, "rows_skipped": 0}
[e2e] in-DB forensics: after=39864 delta=39864 dup_pk_groups=0 pk=(time,robot_id) hypertable=1 tsdb=2.13.1
[e2e] metric naming probe passed: legacy-name prefix 0 hits, canonical name 6 present
e2e stage all passed: 30k landing assertion passed + naming gate present.
```

| Metric | Measured | Scope |
| :--- | :--- | :--- |
| `robot_telemetry` new rows | **39864** | `>= 30000` (ISSUE #60) |
| Stack-up / load duration | 9.1s / 52.5s | 40s controlled duration |
| PK-conflict groups `GROUP BY time, robot_id HAVING count(*)>1` | **0** | must be 0 |
| `rows_submitted` / `rows_written` / `rows_skipped` | 39864 / 39864 / **0** | all equal, skipped=0 |
| `rejected` / `mqtt_dropped` | 0 / 0 | no bad rows |
| hypertable / PK / TimescaleDB | 1 / `(time,robot_id)` / **2.13.1** | real TimescaleDB, not PG substitute |

**This is the first real number for ISSUE #60's "end-to-end 30,000-row landing"** (previously `STATUS.md` §5, `docs/requirements/R-09-database-integration.md` §6 had long carried "unverified").

## Relationship with `test_integration.sh` (Order 06)

`test_integration.sh` runs **bare on the host** and **exits entirely without Docker**, with no CI branch. This directory is its containerized complement: the entry (simulator) also enters a container, the stack can be rebuilt with one command, and it explicitly distinguishes "environment not ready → skip" from "assertion failure → red". The two don't replace each other: the former is a manual integration script, the latter a CI-able test case.
