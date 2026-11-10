# backend_ingestion (Order 03)

High-concurrency ingestion and storage service for 10 Hz telemetry from 100 humanoid robots (FastAPI + aiomqtt + asyncpg + Redis).

## Running

```bash
pip install -r requirements.txt
# local (start infra/docker-compose.yml first)
uvicorn app.main:app --host 0.0.0.0 --port 8000
# Docker (CMD matches local: WORKDIR /app/backend_ingestion + app.main:app)
docker build -t robot-monitor-backend ..
```

## API contract

| Type | Path | Description |
| :--- | :--- | :--- |
| WS | `/ws/robots` | First frame `type:"snapshot"` (100 robots), then `type:"telemetry"` deltas + `type:"alert"` alerts |
| GET | `/robots` | Latest state of 100 robots (fallback outside WS) |
| GET | `/robots/{id}/history?minutes=5&resolution=10s` | Drawer line chart + joint heatmap data |
| GET | `/healthz` | Health check: `status` reflects dependency reachability (`ok`/`degraded`), includes `db` / `retries` / `rejected` / dropped-row triple `rows_submitted|rows_written|rows_skipped` |
| GET | `/metrics` | Prometheus text format (`text/plain; version=0.0.4`), scraped by the `robot_backend` job in `infra/observability/prometheus.yml` |

### WS envelope (spec/00 §5.2, mandatory)

```jsonc
{"type":"snapshot","data":{"robots":[/* RobotTelemetry */]}}
{"type":"telemetry","data":{/* RobotTelemetry */}}
{"type":"alert","data":{"robot_id":"...","rule_id":"...","severity":3,"message":"..."}}
```

> The backend **must** subscribe to Redis `robot:alerts` and forward it as `type:"alert"`, otherwise the frontend alert stream will always be empty.

## Performance guardrails (non-negotiable)

- **Never INSERT row-by-row**: `batch_writer` uses an `asyncio.Queue` with dual triggers (backlog ≥500 rows **or** >1.0s since last flush) to batch writes.
- `asyncio.Lock` protects the flush critical section; `graceful_shutdown()` force-flushes leftovers before closing (wakes the `run()` blocked on `queue.get()` via the `_STOP` sentinel; **do not use cancel**, otherwise the last in-flight batch is lost).
- **Flush must have a hard timeout**: tested locally against PostgreSQL 15.19 — when a table is held by another session's `LOCK TABLE ... ACCESS EXCLUSIVE`, batch writes hang and **ignore** `command_timeout` (even a 5s connection-level timeout still hangs >25s). The consequence of hanging is a silently stalled ingestion pipeline, so all flush batches add an `asyncio.wait_for` cap: better to drop a batch + log an error than to block the consume loop.
- Counting outputs: `/healthz` gives an instantaneous snapshot, `/metrics` gives Counters/Gauges (so Prometheus can compute `rate()`).

### robot_metadata heartbeat (G-6 / ISSUE #96)

The bulk writer for `robot_metadata.is_online / last_seen` is `app/services/metadata_writer.py`, fed from `state_cache.write_and_publish()` (same timing as `liveness.record()`). It follows the same guardrails:

- **Fold by robot_id**: 1000 msg/s (100 robots × 10Hz) collapses to ≤100 rows within a 1s window;
- **Single-statement batch**: `UPDATE ... FROM unnest($1::text[], $2::timestamptz[])`, 1000 telemetry rows → **exactly 1** round-trip (not executemany, and certainly not a for loop);
- **`last_seen` uses the server clock**: not the payload's `ts` (client clock skew / replay / offline buffering would cause the same offline scan to flip a row it just wrote TRUE back to FALSE — hit in practice);
- When the switch `METADATA_HEARTBEAT_ENABLED=false`, `run()` exits immediately and `flush()` issues no statement; the semantics and throughput of the existing telemetry write path are unaffected.
- Counting output: the `metadata` field of `/healthz` (`flushes` / `rows_written` / `offline_marks` / `deduped`).

## Write degradation chain (core design of the write path)

spec/01 §4.2 allows `copy_records_to_table` **or** a bulk `execute`. Locally tested, both achieve 70k~200k rows/s, two orders of magnitude over the 1000 rows/s peak — **throughput is not the selection criterion; fault tolerance is**.

Three hard facts from testing force the implementation to be tiered:

1. `copy_records_to_table` **does not support `ON CONFLICT`**; a repeated batch raises `UniqueViolationError`; but retries exactly re-submit the same batch → idempotency must go through a temp table.
2. Passing a `dict` to COPY raises `TypeError: expected str, got dict` → JSONB columns must be passed as JSON text.
3. **One bad row sinks the whole batch** (COPY / executemany are both batch-atomic).

So a three-tier degradation chain is implemented (see `app/services/batch_writer.py`):

| Tier | Path | Characteristic |
| :-- | :--- | :--- |
| L1 | COPY → temp table → `INSERT ... SELECT ... ON CONFLICT DO NOTHING` | fast, **idempotent**, batch-atomic |
| L2 | `executemany(INSERT ... ON CONFLICT DO NOTHING)` | slower fallback |
| L3 | binary-isolate bad rows | bad rows go to `rejected`, **good data is preserved** |

> Why L3 is necessary: with a batch of 500, one dirty payload, if not isolated, means losing 500 telemetry rows every 0.5s.

`/healthz` and `/metrics` exposed accounting:

| Field | Meaning | Accounting note |
| :--- | :--- | :--- |
| `retries` | Retry count | steady growth means the DB is unstable |
| `rejected` | count of bad rows dropped because L3 judged them unwritable | **non-zero means data loss** |
| `rows_submitted` | rows handed to the storage layer | numerator of the three |
| `rows_written` | rows actually stored | L1 uses `INSERT 0 N` affected rows |
| `rows_skipped` | rows silently dropped by `ON CONFLICT DO NOTHING` | = `submitted - written` |

> Note: the `feat/order-08` / `fix/issue19-merge-safe` branch versions had a `flush_failures` counter, **the main version does not, and this round added none** (undefined means not written, to avoid inventing metrics).

> `rows_skipped` is **direct evidence** that "received data did not all land" (ISSUE #19 G2). Previously `written += len(rows)` counted **submitted rows**, so silently dropped rows were completely invisible — when the count was low you could only guess. Local test (PostgreSQL 15.19): submit 10 rows twice → `submitted=20 / written=10 / skipped=10`, matching the real 10 rows in the DB.
>
> **Known accounting gap**: the L2 path's `executemany` does not return per-row affected rows, so `rows_skipped` is always 0 on L2. Quantify against L1; L2 is only enabled after L1 fails repeatedly (rare).
- MQTT disconnect uses exponential-backoff reconnect (cap 30s), **not a fixed interval**; the `aiomqtt.Client` instance is constructed outside the reconnect loop and reused inside it (official `reconnection.md`: Client is reusable but not reentrant).
- Depends on `aiomqtt==2.5.1`. Previously wrote that "2.0.1's `Client.__aexit__` swallowed the exception and prevented reconnect", **that conclusion is withdrawn** (directionally wrong): source diff v2.0.1→v2.5.0 shows 2.0.1 had `raise exc`, while 2.5.0 **removed** it. Both versions reconnect fine; the real difference is 2.0.1 re-raises and masks the original stack. Evidence in `external/ORDER12-VERIFICATION.md` §2.
- Messages that fail validation are **dropped and counted**, not queued.

## Connection pool (`app/db/session.py`)

`asyncpg==0.32.0` changed `min_size` from "pre-create a few at pool creation" to "idle-time **connection floor**", moving the pre-create semantics to the new param `init_size`. This service's read path (`/healthz` health probe, `/history`) and write path (`batch_writer`) arrive concurrently; two pitfalls were hit, both fixed and guarded:

1. **`max_size` must not equal `min_size`.** When all three are equal the pool **does not scale** under concurrency: locally tested 40 concurrent `acquire()`, the pool size stayed at the set value and the excess coroutines all blocked on `_queue.get()` — the pool degraded into a semaphore. (Proof by contradiction: `min_size=2, max_size=10` grew to 10 in the same scenario.)
2. **Pool creation must be serialized.** `get_pool()`'s `if _pool is None` is check-then-act, while `create_pool()` `await`s and yields the event loop — concurrent callers each build their own pool. The alert engine measured this: the ticker's first round with 100 robots arriving at once → **100 pools** built → `TooManyConnectionsError` **332 times**. Hence an `asyncio.Lock` + double-check was added. (This pit is symmetric on the service side; `alert_engine/app/db.py` fixed it too.)

> Also note: `command_timeout` only covers ordinary statements, and **cannot stop** lock-blocked bulk writes (see above).

## Implementation notes on /history (ISSUE #76 revision)

`spec/00 §4.3`'s `robot_status_10s` **does not include JSONB joint columns**, but the Drawer's heatmap and third line chart need them, so this endpoint previously queried the raw `robot_telemetry` table directly and used `time_bucket`.

ISSUE #76 added a dedicated continuous aggregate **`robot_history_10s`** (`infra/init.sql §5.1`, with `joint_temperatures / joint_positions / joint_torques` columns); `/history` now does: **query the aggregate first → fall back to the raw table if empty**.

- Why JSONB can enter a continuous aggregate: the first arg of `last(value, time)` is **anyelement polymorphic** (timescaledb `src/agg_bookend.c`'s `ts_last_sfunc`), taking the same path as INTEGER; the internal partialize is also `ANYELEMENT` (`sql/partialize_finalize.sql`). The original "missing columns" was simply not written, not unsupported.
- Why not reuse `robot_status_10s`: it is already asserted in three places — R-08 §A4 / `test_integration.sh` / `tests/e2e` — and changing columns would shake the existing acceptance surface; also the two serve different consumers (dashboard latest state vs single-robot history).
- Why keep the fallback: a `WITH NO DATA` aggregate is empty before its first refresh; without fallback the cold-start period would always show an empty curve.
- Each SQL column list is byte-locked by `tests/test_history_source.py` (both branches must not drift, and all three joint columns must appear together) to prevent intermittent failures from "one side adds a column, the other side misses it".
