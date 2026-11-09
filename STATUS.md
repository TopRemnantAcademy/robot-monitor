# STATUS — RobotMonitor master plan and status board

> Last updated: **2026-10-11 (round 2 full reconciliation of 22 branches + 19 dispatched orders)**.
> Dispatch method: see `docs/CNB-DISPATCH-SOP.md` (**§6.5 batch-recovery discipline** is new this round;
> steps 6–8 are round-2 additions: three-point diff, superset check, fake-coverage scan).
> Upstream open-source adoption plan: see `external/INTEGRATION-PLAN.md`.
> Report that re-verified three audit conclusions against real source: `external/ORDER12-VERIFICATION.md`.

---

## 1. Project goal (one sentence)

A **2D real-time monitoring dashboard for 100 humanoid robots** (online status / battery / joint-motor temperature / torque / fault codes),
end-to-end 100 robots × 10 Hz = **1000 msg/s**, with all coding work delegated to CNB's `@codebuddy`.

Explicitly out of scope: 3D CAD, digital twin.

---

## 2. Current real status (verified, not speculated)

| Module | Status | Evidence |
|---|---|---|
| 01 Infrastructure | ✅ Done | `infra/docker-compose.yml`, `infra/init.sql` (seeds 100 robots) |
| 02 Simulator | ✅ Done | `simulator/mock_fleet.py`, measured **1020 msg/s**, 11/11 cases |
| 03 Backend ingestion | ✅ Done | `backend_ingestion/`, static checks 13/13 |
| 04 Alert engine | ✅ Done | `alert_engine/`, rule logic **20/20** measured |
| 05 Frontend dashboard | ✅ **Merged to main** | 34 files / 6696 lines; `tsc --noEmit` **0 errors**, `vite build` **succeeded** (638 modules) |
| 09 DB integration hardening | ✅ **Merged to main** | Real TimescaleDB 2.13.1: `init.sql` ran clean, 30,000 rows landed losslessly, continuous agg 3,100 buckets, DB cases **13/13**. Includes a **three-tier write-degradation chain** (L1 COPY→temp table→`ON CONFLICT` / L2 `executemany` / L3 binary-isolated bad row) and a **JSONB type fix**. See `docs/requirements/R-09-database-integration.md` |
| 06 Integration & stress | ✅ **Recovered** | Evidence in `reports/evidence/` (perf data + 3 real-run logs); `test_integration.sh` hardened with a timeout guard |
| 07 aiomqtt contract (I-1/I-2/I-3) | ✅ **Recovered & verified** | `aiomqtt==2.5.1`, `_STOP` sentinel + `_drained` event in place |
| 08 asyncpg upgrade (I-6) | ✅ **Landed** | `asyncpg 0.29.0 → 0.32.0`; verified `create_pool()` accepts both `init_size` and `min_size` (semantic guard in `app/db/pool.py`) |
| 10 Alert inhibition | ✅ **Recovered & verified** | `alert_engine/tests/` **11/11 passed** |
| Frontend bundle slimming | ✅ **Recovered & verified** | 1209.34 → **671.40 kB** (gzip 401.05 → **224.90 kB**, −44%) |
| 62 Frontend ECharts Bar/Gauge registration | ⏳ **PR #69 awaiting review** | branch `feat/order62-echarts-gauge-bar` @ `62d3fa9`; main chunk 703.50 kB / gzip **235.62 kB** (was 675.19/226.16); `tsc` 0 errors, drawer e2e 8/8, real-browser pixel-level verification of line/bar/gauge |
| Frontend alert-row incremental render | ✅ **Merged** | `AlertLogTable.tsx` / `FleetOverviewCards.tsx` memoized; `tsc` 0 errors |
| Observability (Prometheus/Grafana) | ✅ **Wired and run-verified** | Backend `:8011/metrics` → 200 `text/plain; version=0.0.4`; alerts `:8012/metrics` → 200, measured `robot_alert_emitted_total{rule_id="motor_temp"} 3.0`, `robot_alert_suppressed_total{rule_id="battery_low"} 2.0`, `robot_alert_robots_online_gauge 97.0` (renamed from `robot_alert_robots_online` by order 32) |
| 13 Observability wiring + 6-branch convergence | ✅ **Recovered & verified** | produced `feat/order-13` @ `911b667`; its alert `main.py`/`config.py` judged as regressions (baseline too old) and discarded; main agent added the `alerts_suppressed` count the NPC missed |
| MQTT counter wiring | ✅ **Recovered & verified** | `metrics.py:41,46` defined `mqtt_received`/`mqtt_dropped` but never referenced them (always 0); feeding a bad payload measured `robot_backend_mqtt_dropped_total` going 0 → 1.0 |
| 19 `robot_alert` identity + ack fields | ✅ **Recovered and reverse-verified** | produced `auto/alert-identity-759f` @ `d27b972`; ORM DDL and `init.sql` both literally `PRIMARY KEY (time, alert_id)`; tests 25 → **29 passed**; injected defect measured red |
| Upstream reference source download | ✅ **22/22 complete** (was 14) | `external/fetch-sources.sh` declares 22, disk holds 22, missing/extra both 0; `external/` now **123 MB**. The earlier claim "downloaded 22" was **wrong** |
| 15 Auth & RBAC | ⏳ **In progress at CNB** | issue **#36** / `cnb-e2k-1k4jf5tfo`, baseline `main@58bafb5`, workMode=true verified. 4 stale statements corrected before dispatch |
| 26 Frontend robustness (I-20) | ✅ **Delivered, awaiting recovery** | see issue **#47**. `react-error-boundary@6.1.6` wraps the App outer layer + `@tanstack/react-query@5.104.1` manages Drawer `/history` cache/retry; **does not replace zustand** (query manages server state, zustand manages WS client state). Verified: `tsc --noEmit` 0 errors, `vite build` succeeded, 7/7 new e2e passed |

Local HEAD = CNB `main` = **`58bafb5`** (pushed and synced).
All **22 remote branches** reconciled (round 2); only 5 had genuinely unique contributions, all landed.

> **Round-2 reconciliation conclusion**: after gathering evidence per-branch with `git diff main...cnb/$b` (three-dot),
> the only real new content among the 22 branches was — ① MQTT counter wiring ② 4 lines in `.gitignore`
> ③ 5th re-scan of `external/` ④ `orders/15-18` renumbering for the archive ⑤ the `alert_id` schema proposal from `fix/issue19-p0-gaps`
> (**not yet adopted; reorganized as order 19 dispatched to CNB**).
> All other branches were duplicate or regressed versions of the old baseline; see SOP §6.5 ledger.

---

## 3. Post-cleanup execution plan

### Phase A: Close-out (in progress, handled by CNB)

| # | Task | Owner | Criterion |
|---|---|---|---|
| A1 | Order 06 end-to-end integration + stress report | CNB | `test_integration.sh` repeatable; `robot_telemetry` ≈ 30,000 rows (real SQL count); 7-column report |
| A2 | Order 07 aiomqtt contract alignment | CNB | `aiomqtt==2.5.1`; reused `Client` instance; backoff kept; shutdown order fixed |
| A3 | Order 08 asyncpg upgrade | CNB | `asyncpg==0.32.0`; paste the real `import app.main` output |

> ⚠️ A2 and A3 both edit `requirements.txt`. Agreed in `orders/08-dispatch.md` §6:
> on conflict, the version that includes **both** wins (`aiomqtt==2.5.1` + `asyncpg==0.32.0`).

### Phase B: Main-agent recovery & acceptance (after A; I do this)

| # | Task | Notes |
|---|---|---|
| B1 | **Recover branch per SOP §1 step 7** | `ls-remote --heads` to find branch → fetch → merge. **Do not trust `status: success` alone** |
| B2 | Post-merge regression | `tsc --noEmit` + `vite build`; backend `import app.main` |
| B3 | Conflict handling | 07/08 both edit `requirements.txt`; merge per Phase A rule |

### Phase C: Later enhancements (pending dispatch)

| # | Task | Priority | Basis |
|---|---|---|---|
| C1 | ~~Frontend bundle optimization (ECharts on-demand)~~ **Done** | — | Order 09 landed (→ gzip 224.90 kB); Issue #62 adds Bar/Gauge registration → gzip 235.62 kB, PR #69 pending |
| C2 | Alert inhibition | Medium | `INTEGRATION-PLAN.md` I-5: when offline (severity 3), should not also fire low-battery (severity 2) |
| C3 | Prometheus metric integration (backend `/metrics`) | Medium | Ops layer laid (PR #26); `client_python` downloaded. Close-out action for gaps G-1/G-2 |
| C4 | GitHub sync | Low | Only CNB pushed so far; **`origin` not yet synced** |

### Phase D: Non-functional gaps (PR #26 paved the way; close out one by one)

> Full argument and file:line in `external/INTEGRATION-PLAN.md` §7 and PR #26.

| # | Gap | Current state | Close-out action |
|---|---|---|---|
| G-1 | No `/metrics` across the repo | Only a bare `/healthz` JSON (`backend_ingestion/app/main.py:66-75`); `alert_engine` has no HTTP at all | Add `prometheus/client_python` exposing `/metrics` |
| G-2 | No history curve at 1000 msg/s | `/healthz` is all instantaneous values | Same as above, switch to Counter/Gauge |
| G-3 | "Dashboard fakes green" undetectable | No client-perspective probe | `mqtt-probe` laid; WS synthetic probe landed (`robot_backend_ws_probe_alive` / `robot_backend_ws_probe_failures_total`, ISSUE #58) |
| G-4 | Alert-write failure silent | `alert_engine/app/store.py:53` only `log.exception` | postgres_exporter metrics + Prometheus rule |
| G-5 | Zero backups (~8.64M rows/day) | `infra/ops/pgbackrest/` only has a config contract | Swap in a pgbackrest image or run a sidecar |
| G-6 | `robot_metadata` `last_seen`/`is_online` never written | ✅ **Closed (ISSUE #96)**: batch writer = `backend_ingestion/app/services/metadata_writer.py` (fold by robot_id + single `UPDATE ... FROM unnest`, server clock); `alert_engine` offline rule changed to **prefer** this column, falling back to time delta when unavailable | — |
| G-7 | No multi-robot task-level simulation | `mock_fleet.py` only produces waveforms | `AgentVerse` downloaded (not verified to drive 100 agents) |

---

## 4. Blockers and resolved items

### Remaining blockers (need account-level action)

| # | Blocker | Evidence | Impact |
|---|---|---|---|
| B1 | AI credits exhausted | `402 [EXCEEDING_LIMIT] AI credits exhausted` | NPC cannot continue; root-org admin must raise quota |
| B2 | NPC does not push `main` directly | Measured: with `workMode: true` the NPC can write to the repo, but output lands on a **branch** | This is **by design, not a fault**: the main agent recovers per SOP §6.5 along the path, avoiding old-baseline branches regressing main |

### Resolved this round (measured corrections of earlier misjudgments)

| Previously judged "env blocker" | Actual root cause | Resolution |
|---|---|---|
| ❌ asyncpg fails to compile on Python 3.13 | **Our pinned `0.29.0` was too old** (2023-11); `v0.30.0` (2024-10) officially supports 3.13 | Upgrade to `0.32.0` → dispatched as order 08 |
| ❌ PyPI 403 cannot install deps | `files.pythonhosted.org` blocked, but **mirror works** | `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple` (measured 29s) |
| ❌ Dispatch lacked `--work-mode` | Fixed | Subsequent dispatches all include it; verified `workMode: true` |

---

## 5. Verification boundaries (honestly labeled; do not treat as passed)

### Measured and verified

| Item | Result |
|---|---|
| Frontend type check | ✅ `tsc --noEmit` 0 errors |
| Frontend production build | ✅ `vite build` succeeded (638 modules, 58s) |
| Front/back 24 joint key names | ✅ `telemetry.py:13-22` ↔ `types/robot.ts:8-17` **byte-identical** |
| WS contract | ✅ `/ws/robots` + `snapshot/telemetry/alert` three branches consistent |
| aiomqtt 2.5.1 compatibility | ✅ `app.main` import succeeded; `Client.__init__` params measured `['self','hostname','port','username']` |
| EMQX image same-origin | ✅ Verified: `library/emqx` is built from `emqx/emqx-docker` (official-images `GitRepo` field), same origin as `emqx/emqx`; the earlier "EMQX:5.6 is not EMQX" conclusion is **withdrawn** (wrong comparison granularity) |
| Schema contract (init.sql ↔ ORM) | ✅ `tests/test_schema_contract.py` **4/4 pass**; also did **reverse verification** (injecting a historical defect did make the test FAIL) |
| `test_integration.sh` health parser | ✅ 8 cases measured pass (incl. old array-style output, garbage input, missing fields) |
| Whole-repo pytest (after `pytest.ini`) | ✅ **25 passed** — `tests/test_schema_contract.py` 4 + `alert_engine` 11 + 10. **The earlier 21 was an alert_engine-local count; the root `tests/` 4 were never run** |
| Both services' `/metrics` endpoints | ✅ Backend `:8011` and alerts `:8012` both return 200, `content_type=text/plain; version=0.0.4` |
| `.gitignore` covers `.venv-ci` | ✅ `git check-ignore .venv-ci/pyvenv.cfg` → matches `.gitignore:14:.venv*/` |
| Fake-coverage test files | ✅ Whole-repo scan found **0** (`test_*.py` all contain real `test_*` functions) |
| **End-to-end 30,000-row landing** (ISSUE #60) | ✅ **CI attested 2026-10-13**: PR #111's pull_request build **`cnb-vro-1k4pcqp07-002`** (e2e stage **160.8s**, not SKIP; `python:3.11-slim` + `services: [docker]`). Real TimescaleDB **2.13.1**: `robot_telemetry` **gained 39864 rows** (threshold ≥30000), PK-conflict groups **0**, `rows_submitted=rows_written=39864` / `rows_skipped=0` / `rejected=0`, hypertable=1, PK `(time,robot_id)`. Full log in `tests/e2e/README.md` "real-run record" |
| Frontend error boundary + react-query (order 26) | ✅ `.e2e/run_order26.mjs` **7/7 pass**: child component error caught by ErrorBoundary (no white screen + retry button), `/history` 500 auto-retries until success (backend hit 3×), repeated query within staleTime hits cache instead of backend |

Local verification environment (working): Python **3.13.14** isolated venv,
`aiomqtt 2.5.1 / asyncpg 0.32.0 / fastapi 0.143.0 / redis 8.1.0 / pydantic 2.14.0`.

### Still unverified

| Item | Required condition |
|---|---|
| I-2 / I-3 **runtime** behavior | Needs a real broker; import-time verified |
| Alert dedup idempotency / `robot:alerts` publish | Same as above |

---

## 6. Real defects fixed this round (file:line evidence)

| # | Defect | Location | Consequence |
|---|---|---|---|
| 1 | ~~EMQX image name missing org prefix~~ **(withdrawn)** | `infra/docker-compose.yml` | Original evidence (comparing digests across release channels) did not hold; `emqx:5.6` and `emqx/emqx:5.6` are both EMQX 5.6.1. This round fixed the missing healthcheck `start_period` instead |
| 2 | `/history` does not return `joint_positions` | `backend_ingestion/app/main.py` | Drawer's third line chart "angle (rad)" **always empty** |
| 3 | Redis Hash values stringified | `backend_ingestion/app/services/state_cache.py` | Frontend received `status_code:"1"`, all status colors wrong |
| 4 | Batch write used flush-time `now()` | `backend_ingestion/app/services/batch_writer.py` | PK `(time,robot_id)` conflict → `ON CONFLICT DO NOTHING` **silently drops rows** |
| 5 | Overheat injection clamped by upper limit | `simulator/mock_fleet.py` | Never reaches 82°C (measured max=65.6°C) |
| 6 | **Single bad row in write path sinks the whole batch** | `backend_ingestion/app/services/batch_writer.py` | 1 dirty payload made all 500 telemetry rows vanish (measured 10 good + 1 bad → 0 landed) |
| 7 | **COPY path not idempotent** | same file | `copy_records_to_table` does not support `ON CONFLICT`; retry throws `UniqueViolationError` |
| 8 | **ORM vs in-DB type drift** | `backend_ingestion/app/db/models.py` | ORM emitted `JSON`, `init.sql` is `JSONB` → metadata drift, lost JSONB operator capability |
| 9 | No retention policy, disk only grows | `infra/init.sql` | 8.64M rows/day, hundreds of GB in 30 days; now 30d/90d retention added |
| 10 | `/healthz` does not reflect DB reachability | `backend_ingestion/app/main.py` | DB down still reports `ok`, on-call blind; now returns `db: up/down` + `retries`/`rejected` |
| 11 | Alert write used `pool.execute()` | `alert_engine/app/store.py` | Bad connection could be returned to pool and reused → cascading failures; switched to `acquire()` |

Also: `npm install` once stripped 42 lines of `libc`/`musl` platform markers from `package-lock.json`
(which would make the lock incomplete on Linux CI) — restored the original cross-platform version.

### Order 34: two "fake-green" defects (2026-10-13)

| # | Defect | Location | Consequence |
|---|---|---|---|
| 18 | **crontab e2e omitted `docker.image`** | `.cnb.yml:479` (the `"crontab: 0 3 * * *"` block) | Under this trigger e2e ran on the default image `cnbcool/default-build-env` (**no python3**) → hit 【E2E SKIP】`exit 0` → build 21306ms **all green**, zero rows landed. The same sha (68b7b8b) push build `cnb-152-1k4ohjp3k-002` actually ran on `python:3.11-slim` — the broken declaration was the crontab block, not the push block |
| 19 | **Naming probe used `curl`, unreachable in `python:3.11-slim`** | `.cnb.yml:270,273` (original impl) | `python:3.11-slim` has no curl/wget → `/healthz`/`/metrics` always judged "unreachable" → SKIP. This gate **never actually ran** from the moment it was attached |
| 20 | **Naming probe misjudged "no series" as "metric absent"** | `REQUIRED_BACKEND_METRICS` in `tests/e2e/test_e2e_30k.py` | `robot_backend_batch_flushes_total` has a label; at zero traffic prometheus_client exports no sample line; only exposed after the probe fix (`cnb-dr0-1k4pbtmr7-002` measured) |

Fix: hoist `docker.image` into an anchor `&e2e-image`, referenced by push/PR/crontab; replace `curl` probe with inline `$PY` `urllib`; split label-bearing metrics into `LABELED_BACKEND_METRICS`, allowing only zero-traffic standalone probes to be absent (and logging it); the 30k path requires all present; SKIP paths print a banner `【E2E SKIPPED —— this build did not actually run the 30k landing】`.

### Round-2 detail re-audit (2026-10-10, this round)

User feedback "details completely below standard" triggered a **file-by-file comparison audit** (no reliance on memory, everything actually executed).
Results fall into two classes: **conclusions I had written wrong before**, and **contract drift that had always existed**.

| # | Problem | Location | Nature | Handling |
|---|---|---|---|---|
| 6 | "`emqx:5.6` is a non-EMQX image" — **the conclusion itself was wrong** | `external/SOURCES.md` §1 / `STATUS` / `orders/06` | **Withdrawn** (compared a child manifest digest to an index digest — wrong granularity) | Rewrote the evidence chain, see `external/SOURCES.md` §1.1 |
| 7 | `robot_alert` ORM spuriously added composite PK `(time, robot_id)` | `backend_ingestion/app/db/models.py:52` | **Real contract drift** (init.sql has no such PK) | Aligned + added regression test |
| 8 | `test_integration.sh` used `docker exec` to judge readiness, **bypassing the compose healthcheck** | `test_integration.sh` | Criterion mismatched the object under test | Changed to read `docker compose ps` real health state |
| 9 | EMQX healthcheck missing `start_period` | `infra/docker-compose.yml` | R-07 §1 emphasized this fake-failure risk | Added `start_period: 60s` |
| 10 | Audit script itself reported `robot_metadata` PK as inconsistent | audit script | **Tool not trustworthy** | Fixed script and re-verified: three tables **column names + PK fully consistent** |
| 11 | Repo had **zero automated tests**, R-08 matrix entirely manual eyeballing | whole repo | Contract drift unguarded | Added `tests/test_schema_contract.py` (incl. **reverse verification**) |

> On #6: the most important thing this round is not what was fixed, but admitting that **the "verified" claim written into docs last round was wrong**.
> A digest can only answer "are the bytes identical", not "is it the same project".
> Before comparing digests across release channels, you must first confirm the build source.

### Round 3: 22-branch full reconciliation (2026-10-11)

| # | Problem | Location | Nature | Handling |
|---|---|---|---|---|
| 12 | `mqtt_received` / `mqtt_dropped` **defined but never incremented** | `backend_ingestion/app/metrics.py:41,46` ↔ `mqtt_subscriber.py` | "module exists but unwired", both Counters **always 0** | Added 3 lines of wiring; measured feeding a bad payload drives `dropped_total` 0 → 1.0 |
| 13 | `test_db_integration.py` **collected 0 cases but silently passed** | `backend_ingestion/tests/` | Fake coverage (assertions inside `t1..t6` driven by `main()`) | Renamed to `verify_db_integration.py` + fixed README; whole-repo scan now finds 0 fake-coverage files |
| 14 | Repo had **no pytest config** | whole repo | Root `pytest` would collect `external/` (58 MB third-party source), measured **61 collection errors and abort** | Added `pytest.ini` (`testpaths` + `norecursedirs`) → 25 passed |
| 15 | `.gitignore` could not block `.venv-ci` | `.gitignore:10` | Only wrote `.venv/`; `fix/issue19-p0-gaps` therefore **committed the whole venv by mistake** | Added `.venv*/`; `check-ignore` verified match |
| 16 | `robot_alert` PK relied on `__mapper_args__` stopgap | `backend_ingestion/app/db/models.py:78-86` | ORM identity `(time, robot_id)` conflicts with "same tick can hit multiple rules" semantics; and ORM DDL never matched `init.sql` | Landed by order 19 as a real PK `(time, alert_id)` |
| 17 | "external sources downloaded 22" **was wrong** | `external/` | `external/*` excluded by `.gitignore`, source not in repo, so the claim **could not be checked in the repo at all** | Cross-checked against `fetch-sources.sh`: declared 22 / actually 14 on disk, 8 missing; backfilled to **22/22** (123 MB) |

---

## 7. Documentation index

| File | Content |
|---|---|
| **This file** | Master plan and status |
| `docs/CNB-DISPATCH-SOP.md` | **How to dispatch to CNB** (incl. the process to judge real completion) |
| `external/INTEGRATION-PLAN.md` | Upstream open-source sources → adoption points in this project (I-1~I-12) |
| `external/SOURCES.md` | Provenance and trade-off rationale for the **22** downloaded sources (4th batch in §5, 5th batch re-scanned from scratch in **§6**) |
| `external/INTEGRATION-PLAN.md` | Adoption points I-1~I-12; 5th batch I-11~I-20 in **§8** |
| `orders/NN-dispatch.md` | Per-order dispatch body (latest: **`orders/19-dispatch.md`** `robot_alert` PK convergence) |
| `pytest.ini` | Excludes `external/` from test collection; `testpaths` explicitly the repo's three test dirs |
| `docs/requirements/R-*.md` | Acceptance specs (R-01 frontend / R-02 WS throttling / R-03 API contract / R-07 integration-stress / R-08 acceptance matrix) |
| `tests/` | Repeatable contract tests (`test_schema_contract.py`: init.sql ↔ ORM column-by-column, pure stdlib) |
| `AGENTS.md` | Engineering discipline auto-injected into the NPC each run |
| `.cnb.yml` / `.cnb/settings.yml` | CI pipeline / 10 NPC roles |
