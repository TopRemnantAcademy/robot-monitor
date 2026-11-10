# alert_engine (Order 04)

The **only service that makes business judgments** in the system: it consumes the real-time state that the backend writes to Redis, evaluates alerts based on a sliding window, deduplicates, then writes to TimescaleDB `robot_alert` and `PUBLISH robot:alerts`.

## Running

```bash
pip install -r requirements.txt
python -m app.main
```

## Four rules (all mandatory)

| rule_id | Trigger condition | severity | Dedup window |
| :--- | :--- | :--- | :--- |
| `motor_temp` | any joint > **75°C** AND **≥3 consecutive samples** | 3 critical | 60s |
| `battery_low` | `battery_pct` < **15%** | 2 warning | 120s |
| `offline` | `robot_metadata.is_online = false` (**falls back** to no telemetry > 10s when the column is unavailable) | 3 critical | 30s |
| `joint_stuck` | within a 5s window angular variance < 1e-6 but mean torque > 8.0 | 2 warning | 60s |

## Two driver lines

1. **Consume line** — subscribes to `robot:stream:all`, runs motor_temp / battery_low / joint_stuck.
2. **Ticker line** — traverses all 100 robots every second to run offline. The criterion **prefers reading** `robot_metadata.is_online` (that column is bulk-maintained by the backend's `metadata_writer` heartbeat, see ISSUE #96); when the column is unreadable it falls back to the in-memory `last_seen` time-delta judgment. This engine also low-frequency writes back that column on state changes (existing #61 path).

### Why offline must use a ticker

Hard rule: never rely on Redis Key expiry events:
- Redis's keyspace notification (`notify-keyspace-events`) is **off by default**;
- and after TTL expiry `robot:state:{id}` is already deleted, so the engine cannot even read `last_seen`.

So a **standalone ticker** judges each round, independent of Redis notification mechanisms.

The full set of robot IDs comes from `config.robot_ids` (100 robots, consistent with `init.sql` seed data), ensuring robots that "never came online" can still be judged offline (the backend heartbeat will set their `is_online` to false).

Criterion priority (ISSUE #96, see `app/rules/offline.py`):
1. `is_online is True` → no alert; `False` → alert (the **column is the criterion**, this process no longer computes it itself);
2. `is_online is None` (column not yet written / this engine cannot read the row) → **fall back** to the in-memory `last_seen` time-delta judgment, to avoid misreporting "column not yet written" as 100 robots offline during early deployment.
Switch: `config.offline_use_is_online` (default `True`).

## Inhibition

Modeled on Alertmanager's `inhibit_rules`. Two layers, **prepended** to dedup at the `emit()` entry point:

| Layer | source | target | `reason` | switch |
|---|---|---|---|---|
| **offline** (Order 10) | this robot already judged offline (sev3) | all other alerts on the same robot | `offline` | `config.inhibit_offline` |
| **severity** (Order 30) | the robot's current **highest** severity alert (≥3) | **lower** severity alerts on the same robot | `severity` | `config.inhibit_severity` |

Semantics (Order 30): when a sev3 active alert exists on a robot, its sev2/sev1 alerts are **not stored, not published** — the root cause is upstream (e.g. motor overheating), and derived symptoms (low battery) have no actionable value.

Implementation: `main.py`'s `is_inhibited()` / `inhibition_reason()` + `app/inhibition.py`.

Invariants:

| Invariant | Reason |
|---|---|
| `online_state.get(rid) is False` (**not** a falsy check) | before the ticker completes one round the dict is empty; `not None` would swallow all alerts |
| Inhibition source strictly **higher** than target severity | same-level mutual suppression depends on dict ordering, a race-condition false positive |
| Source ≥ `MIN_SOURCE_SEVERITY` (=3) | if 2, "low battery" would suppress "joint stuck", over-broad |
| Inhibition limited to **the same robot_id** | robot A's high temp should not suppress robot B's low battery |
| offline / the source itself is never inhibited by itself | otherwise the root-cause alert can never fire |
| After the source recovers (registered clear) the inhibition auto-clears | otherwise permanent false suppression |
| Inhibition means drop; no re-issue after recovery | Alertmanager semantics |

### Traceability (Order 30 hard requirement: no silent drops)

Each inhibition produces three pieces of evidence; ops must be able to answer "why didn't this one go out":

1. **Evidence object** `InhibitionRecord` (`app/inhibition.py`): contains `robot_id` / `target_rule` / `source_rule` / `source_severity` / `reason` / `ts_ms`.
2. **WARNING log**: `alert inhibited: reason=… robot=… target=…(sevN) source=…(sevN)`, directly `grep inhibition`-able (uses WARNING not DEBUG — DEBUG is usually unavailable on the ops side).
3. **Standalone metric**: `robot_alert_suppressed_total{rule_id, reason}`, where `reason` distinguishes `offline` / `severity`, so a drop in `alerts_emitted` is attributable.

The active-alert registry `active_alerts` (`ActiveAlertRegistry`) is in-process derived state, same source as `online_state`; the consume line registers on hit and clears on miss, and the ticker clears the whole registry when judging offline.

## Dedup

Redis `alert:dedup:{robot_id}:{rule_id}` + `EX`, `SET ... NX` guarantees idempotency (duplicate messages within the same window are not written twice). After the window expires re-triggering is allowed (re-alert after recovery).

## Joint names

`joint_stuck`'s joint name is taken from the **key of the latest telemetry**, rather than hard-coding the 24 names here again, to avoid drift across the three definitions in spec/00 §5.5 / backend / simulator.

## Store-failure observability (G-4)

In `store.save()` the insert and PUBLISH are **two separate try/except** blocks, each logging only its own failure. As a result, "storage is down" and "no alert at all" look identical from the outside: the frontend alert stream keeps scrolling (publish still succeeds), the dashboard is all green, and only a DB reconciliation reveals the missing data.

Now the insert error branch additionally does `metrics.store_errors.inc()`, exposed as:

```
robot_alert_store_errors_total{...}   # Counter, increments only on store failure
```

Companion rule `AlertStoreFailing` in `infra/observability/rules/robot-alerts.yml`:

```promql
increase(robot_alert_store_errors_total[5m]) > 0     # severity: critical
```

Use `increase` instead of `> 0`: a Counter is monotonic, so `> 0` once triggered never resets (a permanently ringing dead alarm); `increase(...[5m])` only rings when "it failed again in the last 5 minutes", and goes silent on recovery.

Semantic reminder: `robot_alert_emitted_total` rising while `robot_alert_store_errors_total` also rises ⇒ alerts were **genuinely evaluated**, just not stored. This is exactly the fingerprint of a "fake-green" dashboard.

`/metrics` uses the standalone HTTP port started by `metrics.start()` (default 8001), with **no auth at all** (Prometheus is pull-based; adding auth equals silently breaking monitoring); non-`/metrics` paths return 404.

Verification:

```bash
# unit tests (incl. reverse guardrail "remove the instrumentation and it must turn red")
cd alert_engine && python -m pytest tests/test_store_metrics.py -q

# rule metric name <-> code existence
bash infra/observability/verify_wiring.sh
```

## Acceptance evidence (Order 04 / Issue #4)

This directory has two **re-runnable** acceptance artifacts covering orders/04-alert-engine.md §acceptance criteria:

```bash
# ① unit level: 4 rules fire/no-false-positive, offline judgment AST static check, dedup idempotency, store+publish
cd alert_engine && python -m pytest tests/test_rules_acceptance.py -q

# ② end-to-end level (runs without Docker): mock_fleet payload → consume() → rules → dedup
#    → store (fake connection records SQL) → PUBLISH; also drives the real ticker to verify offline
cd alert_engine && python tests/verify_issue04_acceptance.py
```

② The only stubs are three external dependencies (Redis→fakeredis, PG connection pool→recorder, MQTT→directly calling `RobotState` to generate payload); `main.consume` / `main.ticker` / the 4 rules / `deduplicator` / `store` are all real code. Why it's needed: the container chain (`test_integration.sh`) depends on Docker, so in a no-Docker environment the two checks "simulator emits an alert in 30s+" and "offline appears within 10s of shutdown" cannot be verified against EMQX+TimescaleDB, but the engine-side logic can be driven in full.

`tests/test_rules_acceptance.py` also has a **reverse guardrail**: break any rule / dedup / store (invert a threshold, remove NX, swap fetchval for execute, etc.) and the corresponding case must turn red — measured 6/6 all red.
