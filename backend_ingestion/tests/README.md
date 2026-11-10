# backend_ingestion verification scripts

This directory has two kinds of things:

| File | Type | Dependency | How to run |
|---|---|---|---|
| `test_auth.py` | **pytest cases** | install `backend_ingestion/requirements.txt` | `pytest backend_ingestion/tests/` |
| `verify_batch_writer.py` | standalone script | zero dependencies (reads source AST) | `python3 …/verify_batch_writer.py` |
| `test_metadata_writer.py` | **pytest cases** | none (in-memory fake connection) | `pytest backend_ingestion/tests/` |
| `verify_db_integration.py` | standalone script | real PostgreSQL | see below |
| `verify_metadata_heartbeat.py` | standalone script | real PostgreSQL | see below |

## `test_auth.py` (Order 15)

Covers the behavior contract for auth and RBAC: the auth-bypass whitelist is exactly `/healthz` and `/metrics`, no token is always 401 (not 403), WS only verifies the signature without hitting the DB, registration cannot set `role`, and the role inclusion relationship (admin ⊃ viewer). 16 cases, no PG/Redis/MQTT needed.

> ⚠️ **Must be run separately, not from the repo root**:
> This repo has two top-level packages named `app` (`backend_ingestion/app` and `alert_engine/app`); in a single pytest process `sys.modules["app"]` has only one location, so collecting both sets inevitably clobbers each other.
> That is why `pytest.ini`'s `testpaths` deliberately excludes this directory.
>
> ```bash
> pytest backend_ingestion/tests/      # backend side
> pytest                               # root: tests/ + alert_engine/tests/
> ```

> ⚠️ The two `verify_*.py` below are **not pytest cases** (no `test_*` functions);
> running them with pytest collects **0 tests** and silently "passes", creating false confidence.
> `verify_db_integration.py` was originally named `test_db_integration.py` precisely for this reason.

## `verify_batch_writer.py`

Performs **static contract verification** of `app/services/batch_writer.py` and the connection pool (reads source AST, no DB or third-party package needed):

- Degradation chain intact: L1 `_copy_upsert` / L2 `_executemany` / L3 `_isolate_and_write`, the `_STOP` sentinel, the COPY path, `ON CONFLICT` idempotency
- Dropped-row triple wired: `rows_submitted` / `rows_written` / `rows_skipped`
- **L1 must count real stored rows using the affected rows of `INSERT ... ON CONFLICT`** (otherwise `written` counts submitted rows and `skipped` is always 0 — this pit was hit locally)
- Connection-pool guardrails: `MAX_POOL_SIZE` lower bound, `max_size != min_size`, `get_pool()` pool-creation lock

```bash
# repo root
python3 backend_ingestion/tests/verify_batch_writer.py
# or isolated env
/tmp/rm-venv/bin/python backend_ingestion/tests/verify_batch_writer.py
```

Exit code 0 = all passed.

> **History**: this script was previously brought in by `fix/issue19-merge-safe`, verifying that branch's `_to_columns` / `unnest` columnar rework — but that rework **does not exist on main** (main keeps the three-tier degradation chain + `_to_row`). So the script would `AttributeError` on import — a **non-runnable deliverable**. This round replaces it with verifying the logic that actually exists on main.

## `verify_db_integration.py`

Requires a **real PostgreSQL** (TimescaleDB-specific statements are out of scope, see the file header comment):

```bash
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
cd backend_ingestion && python -m tests.verify_db_integration
```

When `TEST_DATABASE_URL` is not set it prints "skipped" and exits 0 — so it should **never** be treated as a CI gate, only as a manual review tool when a DB is available.

Covers 6 scenarios (T1 COPY writes real JSONB / T2 idempotency / T3 degradation chain L1→L2 / T4 bad-row isolation / T5 throughput headroom / T6 ORM DDL matches `infra/init.sql` column types), each mapped to a **real defect that was fixed**.

## `test_metadata_writer.py` (ISSUE #96)

Bulk-semantics cases for the `robot_metadata` heartbeat (in-memory fake connection, no DB needed):

- 1000 telemetry rows (100 robots × 10 times) → **exactly 1** UPDATE, with exactly 100 `robot_id`s in the params (proves "no per-row UPDATE" is held)
- Statement count does not grow with telemetry count (10 rows vs 10000 rows are both 1 statement)
- Offline-scan threshold comes from `settings.state_ttl_seconds`, and **empty windows must also be scanned** (after a robot stops sending, the queue is empty; missing a scan means it never gets set to false)
- The SQL `SET` clause contains only `is_online` / `last_seen` (proves `init.sql` column definitions were not changed)
- **Nothing is written when the switch is off**: with `metadata_heartbeat_enabled=False` not a single statement is issued
- Heartbeats enqueued before `stop()` are not lost

## `verify_metadata_heartbeat.py` (ISSUE #96)

Requires a **real PostgreSQL** (`robot_metadata` is created and seeded per `infra/init.sql`):

```bash
export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/robot_monitor
export JWT_SECRET=test-only-secret
cd backend_ingestion && python -m tests.verify_metadata_heartbeat
```

When `TEST_DATABASE_URL` is not set it prints "skipped" and exits 0 (will not be mistaken for a CI gate).

17 assertions in 6 groups: **A** after running 100 robots' telemetry, `is_online`/`last_seen` have real values (not all NULL / all false); **B** the robot that stops for > `state_ttl_seconds` flips to false without collateral damage; **C** 1000 telemetry rows → 1 UPDATE write-amplification measured; **D** offline rule behavior across the three column states (true / false / fall-back-when-unreadable); **E** no write when the value is unchanged; **F** the directional `WHERE is_online = TRUE` guard for setting false.

Measured result (PostgreSQL 15.19, local, actually run this round): `PASS=17 FAIL=0`, single round-trip 13.3 ms.
