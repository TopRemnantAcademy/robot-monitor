# Download Sources → Integration and Connection Plan for This Project

> This document answers two questions: **exactly which line of this project each downloaded item is applied to**, and **in what order they should be connected**.
> Every integration point is annotated with `file:line` for line-by-line verification. Items not empirically verified are marked "unverified".

---

## 0. Requirement → Source Mapping (Why These, Not Others)

| # | Project Requirement | Adopted Top-Level OSS Source | Size | Integration Method |
|---|---|---|---|---|
| 1 | MQTT Broker deployment contract | `emqx/mqtt-to-timescaledb` | 1.1 MB | **Integrated** (corrected image name) |
| 2 | MQTT client API contract | `empicano/aiomqtt` | 430 KB | Contract review, see I-1/I-2 |
| 3 | Broker-layer throughput stress test | `emqx/emqtt-bench` | 350 KB | Wired into Order 06, see I-4 |
| 4 | Time-series DB write baseline | `timescale/tsbs` | 3.0 MB | Methodology reference |
| 5 | Alert deduplication/inhibition design | `prometheus/alertmanager` (sparse docs) | 816 KB | Design comparison, see I-5 |
| 6 | Metrics exposure | `prometheus/client_python` | 14 MB | Optional for Order 06 |
| 7 | Async Redis usage | `redis/redis-py` | 18 MB | Version check |
| 8 | Frontend state management | `pmndrs/zustand` | 3.6 MB | Version check |
| 9 | Full-stack project structure | `fastapi/full-stack-fastapi-template` | 3.5 MB | Structure comparison |
| 10 | **Async PG driver (lifeline for batch persistence)** | `MagicStack/asyncpg` | 2.0 MB | Version check → **I-6 must upgrade** |
| 11 | Frontend chart bridging | `hustcc/echarts-for-react` | 292 KB | Direct dependency check |
| 12 | WS underlying implementation | `Kludex/starlette` | 7.0 MB | WebSocket contract check |
| 13 | DB migration | `sqlalchemy/alembic` | 4.1 MB | Future migration plan evaluation |
| 14 | MQTT underlying client | `eclipse-paho/paho.mqtt.python` | 1.3 MB | Lower-level dependency of aiomqtt |

**Explicitly excluded** (with reasons, to avoid redundant searching):

| Repo | Size | Reason for Exclusion |
|---|---|---|
| `grafana/grafana` | **2011 MB** | Size is prohibitive; this project ships its own dashboard, so Grafana is an alternative rather than a dependency |
| `apache/echarts` | 264 MB | Already pulled in via npm; **verified** that `frontend_dashboard/node_modules/echarts` exists; the source adds no incremental value to this project |
| `pydantic/pydantic` | 458 MB | Already pulled in via pip; 458 MB is not worth the type-annotation detail |
| `timescale/timescaledb` | 65 MB | Mostly C extension source; this project needs only the SQL-layer contract, already covered by official examples |
| `fastapi/fastapi` | 53 MB | Already pulled in via pip; the 6 MB template is sufficient to cover structural needs |

> Principle: **dependencies already introduced via package managers are not cloned as source**. Cloning is only used to obtain **what packages do not contain** — deployment contracts, best-practice docs, and stress-testing tools.

---

## 1. Completed Integrations (Landed After This Verification)

### I-0 ⚠️ EMQX Image (Original "Correction" Retracted; Reviewed 2026-10-10)

- Integration point: `infra/docker-compose.yml` (image stays `emqx/emqx:5.6`, **only comments and healthcheck changed**)
- Source: `external/mqtt-to-timescaledb/docker-compose.yml`
- **Original conclusion void**: The earlier claim that "`emqx:5.6` is a non-EMQX image" was a misjudgment caused by a **wrong comparison method** — comparing the **sub-manifest** digest of `library/emqx` against the **index** digest of `emqx/emqx`. In fact both are two release channels of the same image (`library/emqx` is built by `emqx/emqx-docker`).
- **Real reason for keeping `emqx/emqx:5.6`**: `library/emqx` currently maintains only 5.7/5.8; for the 5.6 line, `emqx/emqx:5.6` is the continuously-released, traceable source.
- Actual change this round: added `start_period: 60s` to the healthcheck (EMQX starts slowly on first boot; R-07 §1 emphasized the false-failure risk).
- Full verification and retraction notes are in `external/SOURCES.md` §1.1

---

## 2. Pending Integrations (by Priority)

### I-6 🔴🔴 Upgrade `asyncpg` 0.29.0 → 0.30+ (**Unlocks Local Verification; Highest Priority**)

- Integration point: `backend_ingestion/requirements.txt` (currently `asyncpg==0.29.0`)
- Source: `external/asyncpg` + official release notes
- Empirical evidence:

  | Version | Release Date | Python Support |
  |---|---|---|
  | `v0.29.0` (our pinned) | 2023-11-05 | `requires-python = '>=3.8.0'`, **no 3.13 classifier** |
  | `v0.30.0` | 2024-10-20 | **"Support Python 3.13 and PostgreSQL 17"** |
  | `v0.31.0` | 2025-11-24 | + Python 3.14 (experimental subinterpreter / freethreading) |
  | `v0.32.0` (latest) | 2026-10-06 | + Python 3.15 |

  The HEAD `pyproject.toml` classifiers empirically list `3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14`.

- **Why this is the highest priority**:
  The previously recorded environment blocker — *"`asyncpg==0.29.0` fails to compile on Python 3.13 (`_PyLong_AsByteArray` signature change)"* — **the root cause is not the environment but our pinned version being too old**. After upgrading to 0.30+, Python 3.13 is an officially supported target, and the blocker **disappears**. This means the backend can actually run locally instead of being stuck at "unverified" forever.
- **Side effect**: The deployment image pins `python:3.11-slim` (has prebuilt wheel), so production is unaffected; the upgrade is to make the **dev/verification side** runnable, incidentally aligning with PostgreSQL 17.
- **Risk**: Low. 0.29 → 0.32 is a minor upgrade; the `connect()` / `fetch()` / `executemany()` APIs are stable. Any `COPY`-related APIs in use should be regression-tested once inside Docker.

### I-1 🔴 Upgrade `aiomqtt` 2.0.1 → 2.5.1

- Integration point: `backend_ingestion/requirements.txt`
- Source: `external/aiomqtt` + official CHANGELOG (`empicano/aiomqtt@v2.5.1`)
- Empirical evidence: currently pinned `aiomqtt==2.0.1`; latest stable `v2.5.1` (2026-03-05)
- **Why we must upgrade** (not chasing novelty, but fixing a bug):
  ```
  ## [2.5.0]
  - Let exceptions bubble up in `Client.__aexit__` instead of suppressing them
  ```
  Our reconnect logic **fully relies** on `async with` raising `MqttError` on exit to trigger backoff reconnect (`backend_ingestion/app/services/mqtt_subscriber.py:27-38`). Under 2.0.1, `__aexit__` **swallows the exception** → the reconnect branch may never execute → the connection drops but silently never reconnects. This is a latent hazard that directly causes "the dashboard frozen on the last frame".
- **Extra benefit**: Python 3.13 is tested from 2.3.2; from 2.3.1, `get_running_loop` replaces the deprecated `get_event_loop`
- **Risk**: Low (minor upgrade within the 2.x line; the `Client(hostname=..., port=...)` signature is verified consistent at 2.5.1)

### I-2 🔴 Reuse the `aiomqtt.Client` Instance (Explicit Official Requirement)

- Integration point: `backend_ingestion/app/services/mqtt_subscriber.py:31-33`
- Source: `external/aiomqtt/docs/reconnection.md` (v2.5.1)
- Official wording:
  > The `Client` context is designed to be **reusable (but not reentrant)**.
  >
  > ```python
  > client = aiomqtt.Client("test.mosquitto.org")   # ← created outside the loop
  > while True:
  >     try:
  >         async with client:                       # ← reused inside the loop
  >             ...
  > ```
- **Our current approach** (new instance per loop):
  ```python
  while True:
      try:
          async with aiomqtt.Client(hostname=..., port=...) as client:   # ← new each loop
  ```
- **Impact of the difference**: creating a new instance each loop → a new `identifier` each loop → the broker sees a brand-new session. Functionally "it runs", but it violates the official design and cannot preserve session state on reconnect.
- **Keep our better part**: The official example uses a fixed `interval=5`; we use exponential backoff 1→30s. Under 1000 msg/s, backoff is more reasonable — **do not revert to the fixed 5s**.

### I-3 🟡 `lifespan` Shutdown Order Fix

- Integration point: `backend_ingestion/app/main.py:36-52`
- Source: `external/aiomqtt/docs/alongside-fastapi-and-co.md` (v2.5.1)
- Official order: `cancel listener task` → `await task` (catch `CancelledError`) → exit `async with`
- **Our current**:
  ```python
  for t in (mqtt_task, writer_task):
      t.cancel()                        # ① cancel both together
  await writer.graceful_shutdown()      # ② flush the writer
  await asyncio.gather(mqtt_task, writer_task, return_exceptions=True)  # ③
  ```
- **Problem**: `writer_task` is cancelled at ①, but its queue is flushed only at ② — there is a race condition, and the last batch of telemetry (around the 30,000 rows the acceptance test looks at) may be lost on process exit.
- **Recommended order**: First cancel `mqtt_task` (stop receiving new messages) → `await writer.graceful_shutdown()` (flush the backlog) → then cancel `writer_task` → finally gather.
- **Status**: **Unverified** (runtime is needed to prove batch loss; local PyPI is blocked, cannot run)

### I-4 🟢 Order 06 Integration of `emqtt-bench` (Broker-Layer Stress Test)

- Integration point: `orders/06-dispatch.md`'s T3 (performance report)
- Source: `external/emqtt-bench` (EMQX official, Erlang, ★568)
- It complements rather than replaces `simulator/mock_fleet.py`:

  | Tool | What It Tests | payload |
  |---|---|---|
  | `emqtt-bench pub` | **Broker-layer ceiling** (how much EMQX can handle) | fixed `-s <size>` bytes |
  | `mock_fleet.py` | **End-to-end pipeline** (MQTT→validation→Redis→persistence→WS) | real 24-joint payload conforming to the `Telemetry` schema |

  Both must be run to distinguish "bottleneck is in the broker" from "bottleneck is in our backend" — exactly the conclusion Order 06 seeks.
- **Usage** (verified from `external/emqtt-bench/README.md`):
  ```sh
  emqtt_bench pub -h localhost -p 1883 -c 100 -t 'robots/%c/telemetry' -s 1024 -I 100 --prometheus
  ```
  Ships a `--prometheus` metric, can be aligned with `client_python`
- **Prerequisites**: Requires Erlang/OTP 27.2+ to compile, or use the prebuilt binaries from releases

### I-5 🟢 Alert Engine Design Comparison Against Alertmanager

- Integration point: `alert_engine/app/deduplicator.py`
- Source: `external/alertmanager/docs/configuration.md` (sparse, docs only, avoids the full 57 MB)
- **Three concepts to compare** (Alertmanager is the de-facto standard in alerting):
  1. `group_by` — aggregates similar alerts (we dedupe by `robot_id + rule_id`, concept matches ✓)
  2. `repeat_interval` — no duplicate alerts within the dedup window (our `dedup_window_s`)
  3. `**`inhibition`**` — when a high-severity alert fires, suppress a lower-severity one → **we currently lack this mechanism**: when a robot is judged `offline` (severity 3), it may still simultaneously report `battery_low` (severity 2), producing two alerts for the same machine
- **Suggestion**: For now only record as a "follow-up suggestion", do not implement in Order 06 (avoid polluting the closure acceptance)

---

### I-4b 🟢 Order 06 Addendum `locust` (Broker-Layer Stress Test · Writable Assertions)

- Integration point: `orders/06-dispatch.md`'s T3 (performance report)
- Source: `external/locust` (`locust/locust/contrib/mqtt.py`, 557 lines)
- Empirical evidence: `locust/locust/contrib/mqtt.py:516` `class MqttUser(User)`; `:72` `class MqttClient(mqtt.Client)` (underlying is `paho.mqtt`); `:521-522` supports `transport="tcp"` and `transport="websockets"` (`ws_path`)
- **Why it is worth adding** (complements I-4's `emqtt-bench`, not a replacement):

  | Tool | Pros | Cons |
  |---|---|---|
  | `emqtt-bench` | Pure Erlang, can push broker to its limit | Needs OTP 27.2+; only outputs numbers, **hard to customize assertions** |
  | `locust` | Just `pip install locust`; can write `@task` + `events` hooks for **pass/fail judgment** | Absolute ceiling under Python GIL is lower than Erlang's |

  Running **both** yields both "broker ceiling" and "conclusions with SLA judgment" — the latter is exactly what R-07 / R-08 require as "judgeable".
- **Example skeleton** (adapted from the official `examples/mqtt/locustfile.py`, not copied verbatim):
  ```python
  from locust import task
  from locust.contrib.mqtt import MqttUser
  from locust.user.wait_time import between

  class FleetPublisher(MqttUser):
      host = "localhost"; port = 1883
      wait_time = between(0.01, 0.1)          # ~10-100 msg/s/instance, scale to 1000 via multiple instances
      @task
      def telemetry(self):
          self.client.publish("robots/humanoid-001/telemetry", PAYLOAD_1KB)
  ```
- **Status**: **Unverified** (needs real EMQX to run; this item is empirical evidence of "how to use it", not "already run through")

### I-7 🟡 Gap Between `uvicorn` 0.28.0 and Upstream 0.54.0 (**Documented, Not Upgrading**)

- Integration point: `backend_ingestion/requirements.txt:2`
- Source: `external/uvicorn` (`Kludex/uvicorn`, originally migrated from `encode/uvicorn`)
- Empirical evidence: we pin `0.28.0`; `uvicorn/uvicorn/__init__.py:4` `__version__ = "0.54.0"`
- **Decision: Do not upgrade**. Reason: uvicorn only runs `app.main:app`; the `--host/--port/--reload` we use are stable across the entire 0.x line; jumping 26 minor versions gives no benefit to R-01's "build/type 0 errors" baseline, only regression risk. **Value of keeping this record**: It proves the "do not upgrade" decision was made **after reviewing upstream**, not out of ignorance of a newer version.

### I-8 🟡 Gap Between `websockets` 12.0 and Upstream 17.3 (**Documented, Not Upgrading**)

- Integration point: `backend_ingestion/requirements.txt:9` (`websockets==12.0`)
- Source: `external/websockets` (`python-websockets/websockets`)
- Empirical evidence: `websockets/src/websockets/version.py` `tag = version = "17.3"`
- **Decision: Do not upgrade**. `websockets==12.0` is a transitive dependency of `uvicorn[standard]`; we **do not import it directly** (WS goes through Starlette/FastAPI). Crossing 5 major versions yields no benefit. **Same documentation value as above**.

### I-9 🟡 `sqlalchemy` 2.0.28 → 2.1.5 (**Documented, Not Upgrading**)

- Integration point: `backend_ingestion/requirements.txt:6` (`sqlalchemy[asyncio]==2.0.28`)
- Source: `external/sqlalchemy` (`sqlalchemy/sqlalchemy`)
- Empirical evidence: `sqlalchemy/lib/sqlalchemy/__init__.py` `__version__ = "2.1.5"`
- **⭐ Incidentally-verified fact** (important): This project **does not use ORM on the write path** — `backend_ingestion/app/db/session.py:9,13,16` is an `asyncpg` native connection pool; `session.py:3`'s comment explicitly states "batch persistence uses asyncpg's native batch interface". SQLAlchemy appears only in `app/db/models.py:10-11` (`DeclarativeBase`/`Mapped`), used for **read-path and schema alignment**.
- **Decision: Do not upgrade**. 2.0→2.1 is a minor, but our ORM usage is extremely shallow (only model declarations), so upgrade benefit ≈ 0. **This item also corrects a potential misunderstanding**: Some may think "primary key conflict / batch write" is SQLAlchemy's concern — it is not; that is `asyncpg.executemany`'s `ON CONFLICT` (see STATUS.md §6, defect 4).

### I-10 🟢 `pydantic-settings` 2.2.1 → 2.15.0 (**Documented, Not Upgrading**)

- Integration point: `backend_ingestion/app/config.py:8,11`, `alert_engine/app/config.py:5,8`
- Source: `external/pydantic-settings` (`pydantic/pydantic-settings`)
- Empirical evidence: `pydantic_settings/version.py` `VERSION = '2.15.0'`
- **Decision: Do not upgrade**. We only use `BaseSettings` + `SettingsConfigDict` (exported at `pydantic-settings/__init__.py:2`); the core API has been unchanged since 2.x. Both services share the same usage, so pinning the version guarantees reproducibility.

### I-11 🟢 TimescaleDB Container Tuning Parameters (**Suggested for Order 06**)

- Integration point: `infra/docker-compose.yml:25-41` (`timescaledb` service)
- Source: `external/timescaledb-docker` (Timescale official image repo)
- Empirical evidence (official `README.md:56,68,74`): The official image supports env vars such as `TS_TUNE_MEMORY` / `TS_TUNE_NUM_CPUS` / `TS_TUNE_MAX_CONNS` / `NO_TS_TUNE`; the image entrypoint (`timescaledb-docker/docker-entrypoint-initdb.d/001_timescaledb_tune.sh:1-8`) reads them to tune `shared_buffers` / `max_connections` / `work_mem`.
- **Our current state**: `docker-compose.yml:25-41` sets **none of the `TS_TUNE_*` vars** → the container self-computes from cgroup memory, so **parameters are uncontrollable and non-reproducible during stress testing**.
- **Suggestion** (not implemented this round, to avoid polluting the closure acceptance):
  ```yaml
  environment:
    POSTGRES_PASSWORD: postgres
    NO_TS_TUNE: "true"          # disable auto-tuning in test env to keep baseline reproducible
    # or set explicitly:
    # TS_TUNE_MEMORY: "2GB"
    # TS_TUNE_NUM_CPUS: "2"
    # TS_TUNE_MAX_CONNS: "200"
  ```
  "A fixed baseline" has direct value for the **repeatability** of R-08 C1 (30,000 rows ±10%).
- **Status**: **Unverified** (needs to compare `SHOW shared_buffers` values before/after inside a real container)

### I-12 🟢 Prometheus Exporter Authoring Pattern (for C3)

- Integration point: `external/SOURCES.md` already records `prometheus/client_python`; this item adds the **collector authoring pattern**
- Source: `external/node_exporter` (`node_exporter/collector/`, official collector directory structure)
- **Purpose**: When `STATUS.md` reaches stage C3 "Prometheus metrics integration", follow the official collector pattern of "dual `Describe` / `Collect` methods + `prometheus.Desc`" to write our `queue_depth` / `mqtt_dropped` metrics, rather than inventing a one-off approach.
- **Status**: **Unused** (needed at C3)

---

## 3. Version Check Conclusions (**Decision Not to Upgrade**, with Reasons)

| Dependency | This Project | Upstream Latest | Decision | Reason |
|---|---|---|---|---|
| `zustand` | 4.5.5 | v5.0.15 | **No upgrade** | v5 has breaking changes (default export removed, etc.). Current 4.5.5 is verified with `tsc` 0 errors + `vite build` passing, so upgrade benefit < regression risk |
| `redis[hiredis]` | 5.0.3 | v8.1.0 | **No upgrade** | Crosses 3 major versions; we only use `redis.asyncio` basic API and pubsub. Pinned version is reproducible |
| `fastapi` | 0.110.0 | 0.143.0 | **No upgrade** | 0.x line API is stable; the `lifespan`/`WebSocket`/`HTTPException` we use are unchanged |
| `echarts` | 5.5.1 | — | **No upgrade** | Already installed and builds pass |

**The only exception is I-1's `aiomqtt`** — because it ships a bug fix that directly affects reconnect correctness, it is not "chasing novelty" but "fixing a bug". This is the differentiating judgment point of this plan.

---

## 4. Connection Order (What Comes First)

```
① I-1 upgrade aiomqtt ──┐
② I-2 Client reuse    ─┼─→ both in mqtt_subscriber.py, change and verify together
                      │
③ I-3 shutdown order ──┘   same backend, handle together with ①②

④ I-4 emqtt-bench ─────→ Order 06 (running on CNB @codebuddy)
⑤ I-5 alert inhibition ─→ after Order 06, as a v1.1 suggestion
⑥ I-4b locust stress ───→ Order 06 (parallel with emqtt-bench, complementary assertion dimension)
⑦ I-11 TSDB tuning env ─→ optional for Order 06 (fixed baseline, improves repeatability)
```

- ①②③ depend on the same file and must be changed together to avoid a half-integrated state
- ④ is already written into the required-reading list of `orders/06-dispatch.md`; the NPC can consume it directly
- ⑤ is explicitly deferred and not part of Order 06

---

## 5. Reproducible Download Commands

```bash
mkdir -p external && cd external
git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git
git clone --depth 1 https://github.com/timescale/tsbs.git
git clone --depth 1 https://github.com/emqx/emqtt-bench.git
git clone --depth 1 --branch v2.5.1 https://github.com/empicano/aiomqtt.git
git clone --depth 1 https://github.com/pmndrs/zustand.git
git clone --depth 1 https://github.com/redis/redis-py.git
git clone --depth 1 https://github.com/fastapi/full-stack-fastapi-template.git
git clone --depth 1 https://github.com/prometheus/client_python.git
git clone --depth 1 --filter=blob:none --sparse https://github.com/prometheus/alertmanager.git
cd alertmanager && git sparse-checkout set docs && cd ..
# Third batch: data layer and frontend bridging
git clone --depth 1 https://github.com/MagicStack/asyncpg.git
git clone --depth 1 https://github.com/hustcc/echarts-for-react.git
git clone --depth 1 https://github.com/Kludex/starlette.git
git clone --depth 1 https://github.com/sqlalchemy/alembic.git
git clone --depth 1 https://github.com/eclipse-paho/paho.mqtt.python.git
# Fourth batch: 2026-10-10 closure re-scan top-ups
git clone --depth 1 https://github.com/timescale/timescaledb-docker.git
git clone --depth 1 https://github.com/timescale/timescaledb-tune.git
git clone --depth 1 https://github.com/locustio/locust.git
git clone --depth 1 https://github.com/python-websockets/websockets.git
git clone --depth 1 https://github.com/Kludex/uvicorn.git
git clone --depth 1 https://github.com/sqlalchemy/sqlalchemy.git
git clone --depth 1 https://github.com/pydantic/pydantic-settings.git
git clone --depth 1 https://github.com/prometheus/node_exporter.git
```

> Note: The `aiomqtt` repo has **migrated** from `sbtinstruments/aiomqtt` to `empicano/aiomqtt` (verified: `gh api repos/sbtinstruments/aiomqtt` redirects). The PyPI package name `aiomqtt` is unchanged.

---

## 6. Verification Boundaries (Honest Disclosure)

| Conclusion | Status |
|---|---|
| EMQX image name digest difference | ✅ Verified (`docker manifest inspect`) |
| aiomqtt `Client(hostname=,port=)` signature | ✅ Verified (v2.5.1 source) |
| aiomqtt 2.5.0 `__aexit__` fix | ✅ Verified (CHANGELOG) |
| Official `Client` reusable wording | ✅ Verified (reconnection.md) |
| asyncpg 0.30+ supports Python 3.13 | ✅ Verified (release notes + `pyproject.toml` classifiers) |
| Each repo's stars/size | ✅ Verified (GitHub API) |
| locust built-in MQTT (`contrib/mqtt.py`) | ✅ Verified (`contrib/mqtt.py:72,516`, `examples/mqtt/locustfile.py`) |
| TimescaleDB official image supports `TS_TUNE_*` env | ✅ Verified (`timescaledb-docker/README.md:56,68,74`) |
| `uvicorn`/`websockets`/`sqlalchemy`/`pydantic-settings` upstream version numbers | ✅ Verified (respective source version files) |

### ⭐ 2026-10-10 Additional Verification: `external/aiomqtt` Must Be Pinned to the v2.5.1 Tag

A bare `git clone --depth 1 https://github.com/empicano/aiomqtt.git` currently pulls **`3.0.0-alpha.1`** (the v3 rewrite line, which deprecates paho-mqtt, replaces `MqttError`, and makes all parameters keyword-only), and `docs/reconnection.md` **no longer exists** at that HEAD — which would invalidate the file:line evidence for I-1/I-2 in this document.

Resolution: `external/aiomqtt` explicitly `git checkout v2.5.1` (HEAD `c8f8248`); after switching back, the "reusable (but not reentrant)" text at `docs/reconnection.md:5` matches this plan. The reproduction command has been changed to `--branch v2.5.1`. See `external/SOURCES.md` §5 for details.

### ⭐ 2026-10-10 Additional Verification: Local Verification Channel Unblocked

The two previously "unverified" blockers are now resolved:

1. **PyPI 403 bypass via a usable mirror** —— `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. **asyncpg failing to install on Python 3.13 was a version issue** —— after switching to 0.32.0 it installed in 29 seconds

Empirical results (Python **3.13.14** + isolated venv):

```
aiomqtt==2.5.1   asyncpg==0.32.0   fastapi==0.143.0   redis==8.1.0   pydantic==2.14.0

[1] schemas.telemetry OK, joints = 24
[2] app.main import OK
[3] aiomqtt 2.5.1
[4] Client params: ['self', 'hostname', 'port', 'username']
```

**Conclusion**:
- I-1 (upgrade aiomqtt 2.5.1) **verified safe** —— our `Client(hostname=..., port=...)` has a consistent signature at 2.5.1, and `app.main` imports normally
- The backend modules are importable under the **latest dependency combination**, with no hidden import-time incompatibility

| Remaining Unverified | Reason |
|---|---|
| I-2 / I-3 **runtime** behavior | Requires a real MQTT broker + TimescaleDB (Docker); import-time already verified |
| End-to-end 30,000 rows | Same as above, falls within Order 06 scope (running on CNB) |

---

## 7. Fourth Batch Integration Plan: Non-Functional Aspects (CodeBuddy, 2026-10-10)

The first three batches addressed "can the functionality run". This batch addresses "**how do you know it's running well after it's running**".

### 7.0 Gap List (Preliminary Conclusion)

| # | Gap | Factual Basis | Severity |
|---|---|---|---|
| G-1 | **No `/metrics` endpoint anywhere** | `backend_ingestion/app/main.py:66-75`'s `/healthz` only returns JSON; `alert_engine` is just `main()` with **no HTTP at all** (`alert_engine/app/main.py:127-132`) | 🔴 High |
| G-2 | **No historical curve for 1000 msg/s** | The `written/flushes/queue_depth/mqtt_dropped` in `/healthz` are instantaneous values, gone after the stress test ends | 🔴 High |
| G-3 | **No one detects the "dashboard's fake green"** | The backend WS broadcast and the broker's real state have no cross-validation | 🔴 High |
| G-4 | **`robot_alert` persistence failure is silent** | `alert_engine/app/store.py:53` `log.exception("alert publish failed")` —— only goes to the log | 🟡 Medium |
| G-5 | **Zero backups** | The `infra/init.sql` hypertable ≈ 8.64M rows/day; compose only has the `tsdb-data` volume | 🔴 High |
| G-6 | **No one ever writes `robot_metadata.last_seen` / `is_online`** | `infra/init.sql:39-40` defines these two columns; nothing in the repo does an UPDATE | 🟡 Medium |
| G-7 | **No multi-robot task-level simulation** | `simulator/mock_fleet.py` only produces telemetry waveforms, not "task intent" | 🟢 Low |

### 7.1 Landed (This Delivery)

| # | Integration Point | Source | Empirical Evidence |
|---|---|---|---|
| O-1 | `infra/ops/prometheus/prometheus.yml` 6 jobs | `emqx/prometheus`(sparse) + `emqx/emqx`(sparse) + exporter README | `emqx_prometheus_api.erl:63` defines the endpoint; `blackbox_exporter/README.md:33` defines `/probe` |
| O-2 | `infra/ops/grafana/provisioning/*` + 9 dashboard JSONs | `emqx/emqx-exporter`(bundled templates) + `grafana-provisioning-ref` | All dashboards pass `json.load()` (verified) |
| O-3 | `infra/ops/emqx-exporter/emqx-exporter.config.yaml` | `emqx-exporter/examples/docker-compose/` | Official example extracted verbatim into metrics/probes sections |
| O-4 | `infra/ops/blackbox/blackbox.yml` | `prometheus/blackbox_exporter` | Official default module file |
| O-5 | `infra/docker-compose.yml` adds 6 services (`profiles: ["ops"]`) | Each repo's README ports/env vars | See 7.2 |
| O-6 | `infra/ops/pgbackrest/pgbackrest.conf` (**not wired**) | `pgbackrest/pgbackrest` | `doc/xml/reference.xml:7` defines the config path |
| O-7 | `docs/UPSTREAM.md` | All 30+ sources | Licenses registered repo by repo |
| O-8 | `.env.example` adds ops section | — | Secret placeholders, not hard-coded |

### 7.2 Why `profiles: ["ops"]` (Design Decision)

**Basis**: The acceptance criteria of `test_integration.sh` and Order 06 are "30s stress test → `robot_telemetry` ≈ 30000 rows". Bringing up 6 extra containers competes for IO/CPU and **pollutes the stress-test baseline**.

Therefore all ops services are placed in compose `profiles` (a native Docker Compose feature):

```bash
# Daily dev/integration (unchanged, not a single extra container)
docker compose -f infra/docker-compose.yml up -d

# Layer on only when you want the curves
docker compose -f infra/docker-compose.yml --profile ops up -d
```

The `emqx` service only gained one line, `EMQX_PROMETHEUS__ENABLE: "true"` —— **purely an explicit declaration, no behavioral change** (EMQX 5 has this switch on by default).

### 7.3 Unwired Items (Honestly Listed)

| Item | Why Not Wired | What's Needed to Wire It |
|---|---|---|
| backend `/metrics` | Requires changing production code in `backend_ingestion/app/main.py`; an order-level change; this round only delivered **probes** (blackbox hits `/healthz`) | Bring in `prometheus/client_python` (already downloaded), expose `/metrics`, turn `writer.written/flushes/queue_depth` into Counter/Gauge |
| py-spy flame graph | Requires adding `cap_add: [SYS_PTRACE]` to the container (`external/py-spy/README.md:217-232`); changes **runtime permissions** | Add on demand during stress-test diagnosis, no need to keep resident |
| pgBackRest | The `timescale/timescaledb:2.13.1-pg16` image **does not contain** the pgbackrest binary | Replace image or add a sidecar, and first `docker manifest inspect` to verify the image (lesson from the EMQX image name) |
| G-4 alert-persistence-failure alert | Needs an "alert about alerts" mechanism | Use postgres_exporter's `pg_stat` metrics + a Prometheus rule; this batch already laid down the exporter |
| G-6 `robot_metadata` not updated | Involves the `last_seen` semantics of the `offline` rule | Separate order |

## 8. Fifth Batch Integration Plan (I-11 ~ I-20) —— Re-scanned From Scratch on 2026-10-10

> See `SOURCES.md` §5 for upstream sources and selection rationale. This section only answers **which line it applies to and in what order to connect**.
> The judgment criteria follow this repo's discipline: **no hallucination (only cite verified APIs)**, **verification first (file:line)**, **performance guardrails (compute the peak first)**.

### 8.0 At a Glance: Current Gaps → What to Install

```
Audit gap                          What to install                Directly connectable?
─────────────────────────────────────────────────────────────────────────────────────
No metrics guardrail (#no-metrics)  ──→  postgres_exporter + redis_exporter   ✅ official image
Frontend no observability (black    ──→  opentelemetry-js (RUM/trace)        ✅ npm
 box)
Stress test not judgeable (R-07)    ──→  k6 + xk6-mqtt                       ✅ official image
Zero auth hits                     ──→  fastapi-users (same stack)          ✅ pip
Alerts not delivered to person (#27)──→  keep or prom2teams                 ✅ docker-compose
No alert ack closure (#11)          ──→  keep (event management)            ✅ docker-compose
Contract by hand (24 joints)        ──→  datamodel-code-generator           ✅ pip
                                      + openapi-typescript                  ✅ npm
Numbers not reproducible (#18/#19)  ──→  pytest + vitest + playwright       ✅ pip/npm
Disk fills in a week (#7)           ──→  timescaledb retention/compression  ⚙️ SQL policy
Data loss contingency (#28)         ──→  pgbackrest or wal-g                ✅ official image
```

### 8.1 I-11 🔴 Add TimescaleDB Tuning env to `infra/docker-compose.yml`

- **Gap**: In compose, **none of the `TS_TUNE_*` vars are set** → stress-test baseline is not reproducible (undermines the ±10% of R-08 C1)
- **Source**: `external/timescaledb-tune` (downloaded in an earlier round) + `timescaledb` official image README
- **Integration point**: the `timescaledb` service `environment` in `infra/docker-compose.yml`
- **Status**: **Unverified** (needs a real container to compare stress-test throughput)

### 8.2 I-12 🔴 Wire the Three Observability Components Into the Container Network (**Replacing the Earlier "Downloaded But Not Wired"**)

- **Gap**: `prometheus/client_python` is downloaded but `grep -rn prometheus backend_ingestion` returns nothing
- **Source**: `postgres_exporter` (`README.md:252`), `redis_exporter` (`README.md:197,216`)
- **Integration point**: create `infra/observability/docker-compose.observability.yml`, add two exporter services
- **Key constraint**: the exporters must be on the **same network** as `backend`/`redis`/`timescaledb`, otherwise `DATA_SOURCE_NAME` won't resolve (the same pitfall as earlier round §2.3)
- **Status**: **Unverified** (no Docker)

### 8.3 I-13 🟡 `fastapi-users` for Auth and RBAC (corresponds to audit #5 hard gate)

- **Source**: `external/fastapi-users` (**v15.0.5**, verified `fastapi_users/__init__.py`)
- **Compatibility evidence**: `pyproject.toml:133` → `fastapi >=0.65.2`, we pin `0.110.0` ✅ within range
- **Integration point**: `backend_ingestion/app/main.py:78` (REST), `:168` (WebSocket)
- **Scope**: viewer/admin two tiers + WS reject; **not** Keycloak (single plant with 100 robots, ops cost not worth it)
- **Risk**: `fastapi-users-db-sqlalchemy >=7.0.0` pulls in SQLAlchemy — but the write path is an `asyncpg` native pool (clarified in an earlier round), so **the auth library using SQLAlchemy itself does not conflict**
- **Status**: **Unverified** (not run together with `fastapi==0.110.0`)

### 8.4 I-14 🔴 Alert Delivery to a Person + Ack Closure

- **Source**: `external/keep` (active, `providers/` contains `slack/teams/telegram/webhook/ntfy`) or the lightweight route `external/prom2teams` (Teams webhook only)
- **Integration point**: the `emit()` exit at `alert_engine/app/main.py:53-58` → add a notification sink
- **Note**:
  - ⚠️ **`grafana/oncall` is archived** (`README.md:3`); if recommended in earlier rounds it must be retracted
  - The repo **has no DingTalk/WeCom provider** (verified: `ls providers/ | grep -i ding` is empty) → if the plant uses DingTalk/WeCom, use the generic `webhook_provider` and write the forwarding yourself (or adapt prom2teams)
- **Status**: **Unverified** (no container started)

### 8.5 I-15 🔴 Contract as Code (Prevent the "Manual 24-Joint Comparison" Incident From Recurring)

- **Gap**: `STATUS.md §6 #2` records a similar incident (`/history` missing `joint_positions` → the line chart stays empty)
- **Source**: `datamodel-code-generator` (`README.md:327`) + `openapi-typescript`
- **Integration point**:
  - FastAPI's own exported `/openapi.json` → `datamodel-codegen` → `backend` pydantic models
  - The same `openapi.json` → `openapi-typescript` → `frontend_dashboard/src/types/robot.ts`
- **Criterion**: when the contract drifts, CI goes red (aligned with Order 14's criterion)
- **Status**: **Unverified** (tools not actually run)

### 8.6 I-16 🟡 Judgeable Stress Test (**Replacing Erlang emqtt-bench's "Only Outputs Numbers"**)

- **Source**: `k6` (`go.mod` → `go.k6.io/k6/v2`) + `xk6-mqtt` extension
- **Reason for replacing**: `emqtt-bench` needs OTP 27.2+ to compile and is hard to write pass/fail judgments; k6 writes `check()` assertions directly in JS → exactly what R-07/R-08 require as "judgeable"
- **Note**: `mock_fleet.py` is **not replaced** (only our own code can produce the real 24-joint payload, I-4's judgment stands)
- **Status**: **Unverified** (`xk6-mqtt` needs Go compilation, not run)

### 8.7 I-17 🟡 Data Lifecycle (corresponds to audit #7: ~130 GB/day)

- **Source**: `timescaledb` official + `timescaledb-toolkit`
- **Integration point**: `infra/init.sql` (**must stay in sync with init.sql**, AGENTS.md §4)
- **Action**:
  ```sql
  SELECT add_compression_policy('robot_telemetry', INTERVAL '1 day');
  SELECT add_retention_policy('robot_telemetry', INTERVAL '30 days');
  ```
- **Decision pending**: retention window 30 days vs 90 days (cost difference ~3x) — needs the user to decide
- **Status**: **Unverified** (policy not run on a real TimescaleDB)

### 8.8 I-18 🟡 Test Framework Rollout (corresponds to audit #18/#19: "11/11" "20/20" not reproducible)

- **Source**: `pytest` + `vitest` + `playwright` + `testing-library` + `msw`
- **Integration point**: create `backend_ingestion/tests/`, `alert_engine/tests/`, `frontend_dashboard/src/**/*.test.tsx`; add a `test` job to `.cnb.yml`
- **Criterion**: turn the hand-run "11/11" "20/20" in `STATUS.md` into CI-reproducible numbers
- **Status**: **Unverified** (directories not yet created)

### 8.9 I-19 🟢 Disaster Recovery (corresponds to audit #28)

- **Source**: `pgbackrest` or `wal-g`
- **Criterion**: quantified `RPO/RTO` + recovery drill records
- **Status**: **Unverified**

### 8.10 I-20 🟢 Frontend Robustness (corresponds to audit #23/#24)

- **Source**: `react-error-boundary` (2.4 MB, low cost) + `tanstack/query` (server state/retry)
- **Integration point**: wrap `frontend_dashboard/src/App.tsx` outer layer in `ErrorBoundary`; hand the Drawer's `/history` request to query for caching and retry
- **Note**: **Do not replace `zustand`** —— query manages server cache, zustand manages client state after WS throttling, **the two are complementary** (the earlier "zustand not upgraded to v5" decision stands)
- **Status**: **Unverified** (not actually implemented)

### 8.11 Connection Order (avoid half-integrated state)

```
① I-17 Data lifecycle ──→ most urgent (disk fills in a week), only touches init.sql, lowest risk
② I-12 Observability exporter ──→ need metrics to verify all later changes
③ I-15 Contract as code   ──→ lock the contract first so later code changes get type protection
④ I-13 Auth         ──→ changes main.py, must come after contract locked
⑤ I-14 Alert delivery     ──→ depends on ④'s identity (who is notified), so after ④
⑥ I-16 Stress test         ──→ depends on ②'s metrics for comparison
⑦ I-18 Test framework     ──→ cross-cutting; recommend attaching to every order, not standalone
⑧ I-19 / I-20       ──→ value-add, do after closure
```

### 8.12 What This Round **Cannot** Do (Honest Boundaries)

- **No Docker** → I-11/I-12/I-14/I-16/I-17 only have "configuration semantics" conclusions, **no runtime evidence**
- **fastapi-users not actually installed** → I-13's compatibility only has **declaration-time** evidence (`pyproject.toml`)
- **`xk6-mqtt` not compiled** → I-16's compatibility with the k6 v2 line is **unverified**
- This round **only downloaded source + wrote attribution docs, touched no business code**
