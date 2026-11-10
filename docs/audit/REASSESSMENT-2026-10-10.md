# Detailed Reassessment (Issue #19)

> Trigger: The user, across 6 consecutive comments, judged prior outputs as "not thorough enough / details below the bar / completely rough".
> This document does not repeat "which repos were downloaded" — that is a **list**, not an **evaluation**.
> This document answers: **exactly what the platform lacks in detail, at which line, and by what criterion.**
> Every assertion carries a `path:line` or a reproducible command; unproven items are marked "unverified".

---

## 0. Self-Diagnosis First: Why Earlier Rounds Were Judged "Rough"

The user's exact words were "the details are too rough". Reviewing earlier outputs, there are three **structural** problems, not "not written enough":

| # | Symptom | Evidence | Nature |
|---|---|---|---|
| D1 | Delivered a **list** not an **evaluation** | `external/SOURCES.md` is structured around a four-column "repo / size / stars / integration conclusion" table | "What was downloaded" ≠ "where it can be used, how to wire it, what breaks if wired wrong" |
| D2 | Conclusions largely stalled at **"unverified"** | `INTEGRATION-PLAN.md:6` verification-boundary table has 2 of 6 unverified; R-07 §5 treats local non-runnability as a settled fact | The user wants **front-back perfect handoff**, i.e. it actually runs, not "knowing it exists" |
| D3 | Granularity stopped at the **module** level, never reaching **fields / failure modes / timing** | The doc says "I-1 upgrade aiomqtt" but not "how the `dropped` counter's behavior in `_handle` changes after the upgrade" | Insufficient detail = insufficient depth |

Add one **factual correction** (harder than D1~D3):

> **The `external/` directory is currently empty.** Verified in this workspace:
> ```
> $ ls external/
> INTEGRATION-PLAN.md  SOURCES.md
> ```
> Earlier rounds claimed "22 repos already `git clone`d into `external/`", but `external/*` is excluded by
> `.gitignore:24-26`, so **the repo does not store these sources at all**; a fresh workspace clone is necessarily empty.
> Conclusion: those "already downloaded" claims are **not reproducible or checkable** in the current environment.
> This is a real problem — either turn "download" into a reproducible script + checksum manifest that is committed,
> or admit "download" was only a one-time temporary action. See §4 G-20.

---

## 1. Evaluation Method (how this round evaluated)

Evaluated not by "module" but by **each hop of the data flow**. The 100-robot × 10Hz chain has 9 hops:

```
[Simulator] → MQTT → [EMQX] → aiomqtt → [Validation] → Queue → [Batch Persistence] → TimescaleDB
                                              ↘ Redis state → WS → [Frontend store] → Render
                                              ↘ Redis pubsub → [Alert Engine] → Rule → Dedupe → Persist+Publish
```

Each hop asks four questions:
1. **Contract** (field name/type/unit) — are both ends byte-for-byte consistent?
2. **Failure mode** (broken/slow/dirty) — what happens? Any fallback?
3. **Observability** (can problems be localized to this hop)?
4. **Verification** (is there evidence of real execution)?

---

## 2. Per-Hop Evaluation Results

### Hop 1 · Simulator → MQTT (Contract ✅ / Verification ✅)

- The 24-joint key names are consistent in three places, verified (`simulator/mock_fleet.py`, `schemas/telemetry.py:13-22`, `types/robot.ts:8-17`).
- **Detail gap**:
  - **Orientation quaternion is not a valid rotation (verified)**: `mock_fleet.py:135-138`'s `orient_w/x/y/z` each independently `uniform(-1,1)` — their norm **≠ 1**, not a unit quaternion (`|q|² = Σ uniform(-1,1)²` expectation ≈ 1.33, needs sampling to confirm). Consequence: the 7 orientation columns of `robot_telemetry` are **non-zero but physically meaningless**; any downstream (future point-cloud / trajectory replay) is unusable. The current dashboard does not read orientation, so this is a **buried landmine**.
  - `pos_x/y/z` (`mock_fleet.py:132-134`) are `uniform(0,50)/(0,30)/(0,3)`, dimensionally consistent with "factory floor coordinates" ✅, but **not associated with `assigned_station`** — robots at the same station are scattered randomly, so business rules like "same-station clustering" cannot be verified.

### Hop 2 · EMQX Broker (Contract ✅ / Verification ❌)

- The image name was corrected to digest level (`infra/docker-compose.yml:12`).
- **Detail gap**:
  - **No EMQX authentication configured**. `infra/docker-compose.yml:14-21` only sets `EMQX_LOG__LEVEL`. Without `EMQX_AUTHENTICATION` in production, **anyone can pub any `robots/+/telemetry`** — this is the most dangerous class of detail for a monitoring system: forged telemetry can trigger/suppress alerts.
  - **`EMQX_LISTENERS__TCP__DEFAULT__MAX_CONNECTIONS` not set**; 100 clients is fine, **low priority**.
  - The `healthcheck` uses `emqx ping` (`docker-compose.yml:22-24`), which **only proves the container is alive, not that 1883 is subscribable**. R-07 §1 requires "1883 port connectable"; current compose provides no such probe, relying on hardcoded script logic.

### Hop 3 · aiomqtt → Validation (Contract ✅ / Verification ⚠️)

- `Client` reuse was changed per official docs (`mqtt_subscriber.py:32-34`), backoff 1→30s kept (`:38-51`).
- **Detail gap** (newly discovered this round):
  - **`dropped` only increases, never differentiates**. The `dropped` stat in `mqtt_subscriber.py:17` counts "dropped on validation failure", but `_handle` has no branch for any exception other than `ValidationError` — if `state_cache.write_and_publish` throws internally, it is swallowed at `state_cache.py:52-54`, and **not counted in `dropped`**; `/healthz` would show `mqtt_dropped: 0` while data was actually lost. ⇒ "`/healthz` shows 0 dropped" **does not equal** "no data lost". R-03 §2 treats `dropped` as the criterion for "payload invalid", but the implementation cannot distinguish "invalid" from "downstream write failure".
  - **`_STOP` sentinel and `dropped` are both module-level mutable globals** (`mqtt_subscriber.py:17-18`); under multi-instance deployment the counters pollute each other (single instance currently, a **design debt**, not a current bug).

### Hop 4 · Queue → Batch Persistence (Contract ✅ / Verification ✅ at code level)

- Double trigger, `executemany`, `ON CONFLICT`, `_STOP` sentinel all implemented (`batch_writer.py:44-100`).
- **Detail gap**:
  - **`ON CONFLICT (time, robot_id) DO NOTHING` silently swallows conflicting rows**. **Verified as a defect**: `batch_writer.py:104-112`'s `self.written += len(rows)` counts "rows sent", and `executemany` does not return the rows skipped by `DO NOTHING` ⇒ `/healthz.written` is **overstated**, polluting the R-08 C1 "≈30000 rows" criterion. See §4 G-04.
  - **`time` uses the client `ts` (verified)**: `batch_writer.py:114-127` explicitly does `if t.ts: datetime.fromtimestamp(t.ts/1000, tz=utc)`. The design rationale holds (prevents microsecond primary-key collisions within the same batch), but the cost is **ingest time can be specified by the client**, which, combined with an unauthenticated broker, can write to wrong partitions / future moments. See §4 G-04-X, X4.

### Hop 5 · Redis state → WS → Frontend (Contract ⚠️ / Verification ✅)

- Type restoration fixed (`state_cache.py:70-95`'s `_decode`), 100ms throttle verified in shape (`useRobotWebSocket.ts:57-75`).
- **Detail gap** (newly discovered, and a **real defect**):
  - **`/robots` and the WS `snapshot` return `ts` with inconsistent semantics.** `state_cache._state_hash` stores `t.ts or 0` (`:26`), i.e. **client milliseconds**; `_decode` returns it as-is (`:93`). The frontend's `RobotTelemetry.ts:number` (`types/robot.ts:30`), when used in `FleetOverviewCards`/`HealthSummaryHeader` to compute "last update time", relies on the client clock being correct. **Verified the frontend does use it**: `RobotDetailDrawer.tsx:208-209` `new Date(robot.ts).toLocaleTimeString(...)` displays "last update time". ⇒ Client clock drift directly displays a wrong "last update", causing the operator on duty to misjudge online status. See §4 G-09.
  - **When alerts flow through WS forwarding, `ts` is in milliseconds** (`store.py:56` `int(time.time()*1000)`), and telemetry `ts` is also milliseconds — consistent ✅. But **`robot_alert.time` in the DB uses `datetime.now(timezone.utc)`** (`store.py:36`), i.e. **server clock**, so "the same alert" may differ by milliseconds to seconds between DB and WS (client clock drift). ⇒ When troubleshooting, "aligning DB and frontend by time" won't match. See §4 G-05.
  - **WS has no backpressure**. `main.py:199-205` directly `await ws.send_json(...)`. If a browser tab is suspended (`setInterval` not running, TCP window full), `send_json` blocks, and since it is **the await in the subscription loop** → all messages behind that connection get stuck. Fine with a single client; a **avalanche point** with multiple/slow clients. See §4 G-06.

### Hop 6 · Alert Engine · Rules (Contract ✅ / Verification ✅)

- 4 rules + dedup window complete (`alert_engine/app/main.py:33-37`).
- **Detail gap**:
  - **`window_maxlen=100` is inconsistent with `joint_stuck_window_s=5.0`**. `config.py:39` window is 100 entries, at 10Hz ⇒ 10s; but `joint_stuck` only needs a 5s window (`config.py:32`). Currently the implementation is larger than the requirement, so it **won't miss** anything, but **the meaning is confusing**: the window comment says "about 10s" while the rule actually only uses 5s; future sampling-rate changes will step on this.
  - **`motor_temp`'s "3 consecutive samples" is verified correct**: `rules/motor_temp.py:24-33` takes `list(window)[-3:]` (window appends in arrival order) ⇒ indeed 3 consecutive entries. `joint_stuck` needs 50 samples ≤ `window_maxlen=100`, capacity sufficient. The only gap is `config.py:39`'s comment not matching its use. See §4 G-07.

### Hop 7 · Alert Engine · Persistence and Publish (Contract ✅ / Verification ⚠️)

- Persistence + `PUBLISH robot:alerts` both implemented (`store.py:28-60`).
- **Detail gap**:
  - **`save()` still `publish`es after DB write fails** (`store.py:37-58` two independent try blocks). I.e.: **DB has no such alert, but the frontend shows it**. Under the semantics of "alerts must not be lost", this is **dangerous**: the operator sees the alert on the frontend, but later audit of the DB finds no such row. Needs to clarify whether it is "persist first then publish" or "publish = promise" — currently it is "best effort", with no compensation. See §4 G-08.
  - **`robot_alert` table's `ack` field is never written** (`infra/init.sql:60`). R-03/frontend `AlertLogTable` both have a shadow of the "unacknowledged" concept, but **there is no ack interface**. `HealthSummaryHeader`'s "current alert count (severity≥2 unacknowledged)" — unacknowledged is always all of them. See §4 G-09.

### Hop 8 · Frontend Render (Contract ✅ / Verification ✅)

- 100ms throttle, backoff, three-envelope branch all implemented with sufficient comments.
- **Detail gap**:
  - **Alerts are not deduplicated/merged**. `robotStore.pushAlert` (`store.ts:101-102`) is pure prepend, `ALERT_BUFFER_LIMIT=200`. If the backend's dedup window expires and the alert repeats, the frontend will **pile up duplicate rows**.
  - **No virtual scrolling**. 100 cards + 200 alert rows is OK for now, but the **card wall renders all 100 DOM nodes with `aspect-[4/3]`**, perceptible on low-end large-screen machines. A **performance debt**, not a current blocker.
  - **Drawer history request race**: `RobotDetailDrawer.tsx:55-66` **already uses `AbortController`**, the race is correctly handled ✅. Not counted as a gap (see §4 G-12, downgraded).

### Hop 9 · Ops/Delivery Surface (the thinnest layer)

The first 8 hops at least exist (though details are missing); **the 9th hop is almost empty**:

| Enterprise capability | Status | Evidence |
|---|---|---|
| Auth/RBAC | **None** (R-03 §1 explicitly says "no auth") | `docs/requirements/R-03-backend-api-contract.md:9` |
| Audit log (who changed thresholds / who acked an alert) | **None** | No corresponding table/interface |
| Data retention policy (TimescaleDB retention) | **None** | `infra/init.sql` has no `add_retention_policy` |
| Backup/recovery | **None** | No scripts; `tsdb-data` is an anonymous volume |
| Observability (Prometheus / structured logs / trace id) | **None** (`client_python` downloaded but unused) | `docker-compose.yml` has no exporter |
| Deployment orchestration (production-grade, not compose dev) | **None** | Only dev compose, no resource limit/restart policy |
| Alert notification outlet (SMS/DingTalk/PagerDuty) | **None** | Only PUBLISH to Redis |
| Multi-tenant/multi-factory | **None** (`assigned_station` is just a string label) | `infra/init.sql:48` |
| SLO/capacity planning | **None** | None |
| Config validation (fail-fast at startup) | Partial | pydantic-settings has it, but no port/URL validity check |

> This layer is the **true meaning** of the user's "what a large enterprise needs".
> Earlier rounds scattered them across "Orders 11-14", but **none gave implementation-level granularity** — equivalent to not writing them.

---

## 3. Gaps the User "Didn't Think Of" (that I thought of for them)

Sorted by "will cause an incident if not fixed", each with **trigger condition → consequence → minimal fix**:

| # | Scenario | Trigger condition | Consequence | Minimal fix |
|---|---|---|---|---|
| X1 | **Forged telemetry** | EMQX has no auth (`docker-compose.yml:14-21`) | Can flood fake data, can create/suppress alerts | EMQX enables `authentication` + backend connects with credentials |
| X2 | **Alert "on frontend, not in DB"** | `store.py` publish and insert decoupled | Audit mismatch, operator misjudges | Insert successfully first then publish, or publish after persist failure but log ERROR |
| X3 | **Disk fills up** | No retention, 1000 msg/s ≈ ~8.6 billion rows/day | DB overflows, writes fail, dashboard stops updating | `add_retention_policy('robot_telemetry', INTERVAL '30 days')` |
| X4 | **Clock drift causes wrong partition** | If `time` takes client `ts` (to confirm) | Data lands in wrong chunk, query misses data | Unify to server receive time (or enforce NTP + offset check) |
| X5 | **Slow client drags down WS broadcast** | `main.py` has no backpressure | One suspended tab blocks that connection | Outbound queue + drop oldest / `asyncio.wait_for` timeout |
| X6 | **Restart loses "online" judgment** | `last_seen` is in memory (`main.py:41`) | Alert engine restart misjudges all machines offline within 10s | Cold-start grace period, or restore `last_seen` from DB/Redis |
| X7 | **Missing ack semantics** | `robot_alert.ack` never written | "Unacknowledged alert count" always = all, KPI distorted | Add `PATCH /alerts/{id}/ack` + frontend button |
| X8 | **Config drift** | 3 configs (backend/alert/frontend env) each written separately | Channel name/threshold inconsistency silently fails | Extract common defaults, or cross-validate at startup |
| X9 | **No CI regression guardrail** | No unit tests seen running in pipeline | Any change can silently break the contract | Turn the 24-joint consistency in §2 into a CI assertion |
| X10 | **Missing backup** | `tsdb-data` anonymous volume | Volume deleted = all history lost | `pg_dump` scheduled + named volume + recovery drill |

---

## 4. Gap List (actionable, with acceptance criteria)

> Each item gives: ID / one-liner / evidence coordinate / acceptance criterion / priority.
> Priority: P0=incident if not fixed, P1=roughness unacceptable but schedulable, P2=optimization.

### G-01 [P0]EMQX Authentication Missing
- Evidence: `infra/docker-compose.yml:14-21` has no `EMQX_AUTHENTICATION`
- Criterion: MQTT client pub without credentials is rejected; backend with credentials can connect; `test_integration.sh` adds an "anonymous rejected" case

### G-02 [P0]Alert Persistence/Publish Consistency
- Evidence: `alert_engine/app/store.py:28-60` two independent try blocks
- Criterion: DB write failure logs ERROR and `/metrics` has a counter; docs state "publish does not promise durability" or change to persist-first-then-publish

### G-03 [P0]Data Retention Policy
- Evidence: `infra/init.sql` has no `add_retention_policy`
- Criterion: `SELECT * FROM timescaledb_information.jobs` contains a retention job; expired chunks deleted

### G-04 [P0]Ingest Timestamp Source and `written` Count Semantics (**verified, confirmed defect**)
- Evidence A (`written` count inflated): `batch_writer.py:104-112`
  ```python
  await conn.executemany(INSERT_SQL, rows)
  self.written += len(rows)     # ← counts "rows sent", not "rows actually inserted"
  ```
  `INSERT_SQL` carries `ON CONFLICT (time, robot_id) DO NOTHING` (`batch_writer.py:35`),
  **`executemany` does not return the rows skipped by conflict**. Therefore `/healthz.written`
  is **overstated** when there are conflicts, polluting the R-08 C1 "≈30000 rows" criterion — it only proves "30000 sent",
  not "30000 stored". **Must** cross-verify with a separate `SELECT count(*)`.
- Evidence B (timestamp from client, **forgeable**): `batch_writer.py:114-127`
  ```python
  if t.ts:
      ts = datetime.fromtimestamp(t.ts / 1000.0, tz=timezone.utc)   # ← client-provided milliseconds
  else:
      ts = datetime.now(timezone.utc)
  ```
  The code comment explains "why use client ts" (avoid same-batch same-robot microsecond primary-key collision); this reason **holds**;
  but the cost is **ingest time can be arbitrarily specified by the client** — combined with G-01 (EMQX no auth),
  data can be written into any time chunk (including future moments), affecting continuous aggregates and retention.
- Criterion:
  1. `/healthz` adds two counters `inserted` (truly inserted) and `conflicted` (skipped), implemented via `INSERT ... RETURNING` or before/after `count(*)` deltas;
  2. `test_integration.sh`'s 30000 criterion changes from "`SELECT count(*)`" to "`/healthz.written`";
  3. Docs state "ingest time uses client `ts`, treated as UTC milliseconds; server does not validate offset", and after G-01 is fixed, the broker layer guarantees client trustworthiness.

### G-05 [P1]Single Source of Truth for Timestamps
- Evidence: `store.py:36` (DB uses server time) vs `store.py:56` (WS uses local milliseconds)
- Criterion: Same alert DB.time vs WS.ts difference <100ms (measured); or unify by having DB `RETURNING time` backfill the WS payload

### G-06 [P0]WS Backpressure
- Evidence: `backend_ingestion/app/main.py:199-205`
- Criterion: Simulate a client that does not read the socket; other clients' latency unaffected (measured p95 offset <50ms)

### G-07 [P1]`motor_temp` / `joint_stuck` Window Semantics (**verified, conclusion: implementation correct, only comment needs fix**)
- Evidence: `rules/motor_temp.py:24-33` takes `list(window)[-min_samples:]`, window appends in arrival order, so it is indeed "the most recent 3 **consecutive** samples" — **judgment correct**.
- Evidence: `rules/joint_stuck.py:31` `need_samples = max(2, int(5.0*10)) = 50`, and `config.py:39` `window_maxlen = 100` ⇒ 50 ≤ 100, **capacity sufficient, judgment correct**.
- The only remaining gap is **misleading comment**: `config.py:39` comment says "about 10s", while `joint_stuck` only uses 5s and `motor_temp` only uses 3 samples — capacity and purpose naming inconsistent.
- Criterion: Add a comment to `window_maxlen` explaining "take the max of all rules' required time windows + margin", and add a unit test locking "motor_temp needs 3 consecutive samples, joint_stuck needs 50", to prevent silent failure when sampling rate changes.

### G-08 [P1]Alert Ack Closure
- Evidence: `infra/init.sql:60` (`ack` column never written)
- Criterion: `PATCH /alerts/{id}/ack` takes effect; frontend "unacknowledged count" drops as acks happen (measured)

### G-09 [P1]Clock/Timezone Discipline
- Evidence A: `infra/init.sql:12` (`TIMESTAMPTZ`) ✅, but the client `ts` → `TIMESTAMPTZ` conversion is undocumented
- Evidence B (**real defect**): frontend `RobotDetailDrawer.tsx:208-209` `new Date(robot.ts).toLocaleTimeString(...)` — "last update time" renders directly using **client milliseconds**, while `robot.ts` ultimately comes from the simulator/robot's local clock. Clock drift ⇒ operator sees a wrong "last update".
- Criterion:
  1. Docs state "ingest is always UTC, client `ts` treated as UTC milliseconds";
  2. "Last update time" uses **server receive time** (or frontend computes using `Date.now()` vs local receive time delta), not trusting the remote clock;
  3. Add a guard "if `ts` is outside [now-1d, now+1min], discard and count" (also mitigates G-04-X).

### G-10 [P1]`/healthz` Metrics Insufficient for Localization
- Evidence: `backend_ingestion/app/main.py:72-82`
- Criterion: Add "drop counts by reason" (validation failure / downstream failure / queue full) and "last successful persist time"

### G-11 [P1]Redis Loss Is Silent
- Evidence: `state_cache.py:52-54` (exception swallowed), `main.py:172-175` (snapshot failure only logged)
- Criterion: When Redis unavailable, `/healthz` should return `degraded` not `ok`

### G-12 [P2]Frontend Drawer Request Race (**verified, conclusion: handled, no fix needed**)
- Evidence: `RobotDetailDrawer.tsx:55-66` already uses `AbortController`:
  ```ts
  const abortRef = useRef<AbortController | null>(null);
  const controller = new AbortController();
  fetch(url, { signal: controller.signal })
  ```
  cleanup aborts the old request — **race correctly handled**. Downgraded/removed from the gap list, only kept as a "better with a test locking it" suggestion.

### G-13 [P1]Simulator Orientation Invalid (buried landmine)
- Evidence: `simulator/mock_fleet.py:135-138` quaternion components each independently `uniform(-1,1)`, norm ≠ 1
- Consequence: `robot_telemetry`'s `orient_w/x/y/z` are non-zero but **not a valid rotation**; current dashboard doesn't read them, but any future trajectory/replay feature is unusable
- Criterion: Generate a **unit** quaternion using "random axis + random angle" (`|q| = 1`, float error <1e-6), and add an assertion; associate `pos_*` with `assigned_station` (same-station clustering)

### G-14 [P2]Frontend Alert Merging and Virtual Scrolling
- Evidence: `robotStore.ts:101-102`
- Criterion: No duplicate rows within the `(robot_id, rule_id)` window; 100-card render frame time has measured numbers

### G-15 [P1]Observability (Prometheus)
- Evidence: `docker-compose.yml` has no exporter; `client_python` downloaded but unused
- Criterion: `/metrics` exposes `mqtt_received/dropped`, `batch_flush_seconds`, `queue_depth`; scrape succeeds

### G-16 [P1]CI Contract Regression
- Evidence: No CI asserts 24-joint consistency
- Criterion: One CI test reads both `telemetry.py:13-22` and `types/robot.ts:8-17` and asserts equality (fails if unequal)

### G-17 [P1]Production-Grade Deployment Orchestration
- Evidence: `infra/docker-compose.yml` has no `restart` / `deploy.resources` / log rotation
- Criterion: Process crash auto-restarts; `max-size` log cap; docs state production vs dev differences

### G-18 [P2]Alert Notification Outlet
- Evidence: Only PUBLISH to Redis
- Criterion: At least one external outlet (Webhook) configurable, with bounded retry on failure

### G-19 [P2]Multi-Factory/Multi-Tenant
- Evidence: `init.sql:48` `assigned_station` is only a label, no permission isolation
- Criterion: Explicitly state "currently single-factory scope" and write it into R-08, to avoid the acceptor mistakenly assuming support

### G-20 [P1]`external/` Download Not Reproducible
- Evidence: This workspace `ls external/` has only 2 md files; `.gitignore:24-26` excludes sources
- Criterion: Provide `external/fetch-sources.sh` (each repo pinned to a tag/commit), one-click reproducible in any environment; or explicitly declare "download is one-time reference only, not committed"
- **Handled this round**: Added `external/fetch-sources.sh` (21 sources + alertmanager sparse), each pinned to an explicit ref; after execution, writes "local HEAD commit" into `external/.fetch-log` as evidence. Verified (2026-10-10):
  ```
  $ bash external/fetch-sources.sh echarts-for-react
  echarts-for-react  2346edc  HEAD          ← matches the commit claimed in PR #20 (reproducible)
  $ bash external/fetch-sources.sh aiomqtt
  aiomqtt            c8f8248  v2.5.1        ← matches the v2.5.1 HEAD claimed in PR #20
  $ head -6 external/aiomqtt/docs/reconnection.md
  The `Client` context is designed to be reusable (but not reentrant).   ← byte-identical to I-2 reference
  ```
  ⇒ The two core assertions in PR #20 about aiomqtt version drift **are independently reproducible**.
  Still missing: SHA256 checksum (upstream force-push would false-report, so the script pins ref not hash).

### G-21 [P0]End-to-End Real-Run Evidence
- Evidence: R-07 §5 treats "local non-runnable" as settled; Order 06 still running on CNB
- Criterion: `test_integration.sh` actually runs in at least one environment and pastes a 7-column report (with real SQL counts)

---

## 5. Relationship to Earlier Rounds (avoid duplicated work)

- Parts covered and **valid** by earlier rounds (not overturned): EMQX image name digest correction, aiomqtt 2.5.1 contract, asyncpg version root cause, `lifespan` shutdown order, ECharts tree-shaking.
- New this round: §2 per-hop details, §3 X1~X10, §4 G-01~G-21.
- **Corrected** hard facts this round: `external/` sources not committed ⇒ the claimed 22 downloads not reproducible (G-20); and **overturned two of our own misjudgments**: G-07's `motor_temp` implementation was correct, G-12's frontend race was already handled.

---

## 6. Suggested Execution Order (not "keep finding repos")

```
Batch 1 (stop the bleeding, P0): G-01 G-03 G-04 G-06 G-21
Batch 2 (lock contract, P1):     G-02 G-05 G-07 G-09 G-10 G-11 G-13 G-16 G-20
Batch 3 (close loop, P1):        G-08 G-15 G-17
Batch 4 (experience/scale, P2):  G-12 G-14 G-18 G-19
```

> The only true sign of "details up to bar": `test_integration.sh` runs one-click in a clean environment, every column of the 7-column report has real commands and output, and every hop in §2 has all four of "contract / failure mode / observability / verification". **Until then, any "done" is a verbal promise.**

---

## 7. Verification Boundaries of This Document (honest disclosure)

| Assertion | Status |
|---|---|
| `external/` currently empty | ✅ verified (`ls external/`) |
| EMQX no auth config | ✅ verified (read `infra/docker-compose.yml`) |
| `init.sql` no retention policy | ✅ verified (read full text) |
| `ack` column no writer | ✅ verified (grep all repo for `ack`) |
| WS no backpressure | ✅ verified (read `main.py` WS branch) |
| G-04 `written` overstated | ✅ verified (`batch_writer.py:104-112`) |
| G-04 timestamp from client | ✅ verified (`batch_writer.py:114-127`) |
| G-07 `motor_temp` 3 consecutive samples correct | ✅ verified (`rules/motor_temp.py:24-33`) |
| G-07 `joint_stuck` 50 samples ≤ window 100 | ✅ verified (`rules/joint_stuck.py:31` + `config.py:39`) |
| G-12 frontend race uses AbortController | ✅ verified (`RobotDetailDrawer.tsx:55-66`) |
| `ack` only frontend-filtered, no backend interface | ✅ verified (`HealthSummaryHeader.tsx:45` comment + grep all repo for ack-write interface) |
| Frontend "last update time" relies on client `ts` | ✅ verified (`RobotDetailDrawer.tsx:208-209` `new Date(robot.ts)`) |
| Any "already run through" conclusion | ❌ None — no Docker in this environment, this round **ran no services** |

> Unverified items are marked at their respective entries and **must not** be cited by later work as confirmed.
