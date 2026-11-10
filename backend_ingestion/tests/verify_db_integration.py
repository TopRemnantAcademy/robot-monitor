"""End-to-end DB integration verification (real PostgreSQL, not mock).

Run (needs a writable PostgreSQL with the pure-PG part of infra/init.sql executed):

    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
    cd backend_ingestion && python -m tests.verify_db_integration

WARNING: this file is **not** a pytest case; the original name `test_db_integration.py`
has been dropped: all assertions live in `t1..t6`, driven by `main()`, so pytest
collects **0 tests** -- a name with `test_` would create the false impression of
"tests running" (the same false-coverage defect as alert_engine's `test_notifier.py`).
It is a **standalone script run on demand**, exit code 0 = all passed.

Coverage (each corresponds to a real defect fixed this round, not padding assertions):
  T1 COPY path writes a real JSONB table, and the jsonb column type is not corrupted
  T2 COPY path is idempotent (repeated batches don't add rows, don't error) -- this is
     the patch for COPY having no ON CONFLICT
  T3 Degradation chain: force L1 to fail -> L2 takes over
  T4 Bad-row isolation: one dirty row doesn't drag down the whole batch
  T5 Write throughput redundancy multiple vs the 1000 rows/s peak
  T6 ORM model DDL column types match infra/init.sql (JSONB drift regression)

Statements needing the timescaledb extension (create_hypertable / continuous aggregate /
retention policy) are out of scope here; they are marked "unverified" with the
verification conditions written in that file's comments.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "")
if DSN:
    # Must be injected before importing app.config: settings is a module-level singleton,
    # changing database_url afterward will not affect the already-constructed Settings.
    os.environ["DATABASE_URL"] = DSN

from app.db.models import RobotTelemetry  # noqa: E402
from app.schemas.telemetry import Telemetry  # noqa: E402
from app.services.batch_writer import COLUMNS, BatchWriter  # noqa: E402

from app.config import settings  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def make_telemetry(i: int, robot_id: str | None = None) -> Telemetry:
    return Telemetry.model_validate(
        {
            "robot_id": robot_id or f"humanoid-{i % 100 + 1:03d}",
            "status_code": 1,
            "error_code": 0,
            "battery_pct": 88.0,
            "battery_temp": 35.0,
            "max_joint_temp": 42.5,
            "joint_temperatures": {"left_shoulder_pitch": 42.5},
            "joint_positions": {"left_shoulder_pitch": 0.1},
            "joint_torques": {"left_shoulder_pitch": 3.2},
            "pos_x": 1.0, "pos_y": 2.0, "pos_z": 0.5,
            "orient_w": 1.0, "orient_x": 0.0, "orient_y": 0.0, "orient_z": 0.0,
            # key: client millisecond timestamps must differ, otherwise primary-key
            # conflicts would silently drop rows
            "ts": 1767225600000 + i,
        }
    )


async def t1_copy_jsonb(conn: asyncpg.Connection) -> None:
    print("\nT1 COPY path writes a real JSONB table")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(200)]
    await w._copy_upsert(rows)
    n = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("row count correct", n == 200, f"{n}/200")
    types = await conn.fetchrow(
        """SELECT jsonb_typeof(joint_temperatures) jt, jsonb_typeof(joint_positions) jp,
                  jsonb_typeof(joint_torques) jto FROM robot_telemetry LIMIT 1"""
    )
    check("the three joint columns are still jsonb (not degraded to text/json)",
          tuple(types) == ("object", "object", "object"), str(dict(types)))
    v = await conn.fetchval(
        "SELECT joint_temperatures->>'left_shoulder_pitch' FROM robot_telemetry LIMIT 1")
    check("jsonb is queryable via ->> (prerequisite for continuous aggregate / heatmap)",
          v == "42.5", str(v))


async def t2_idempotent(conn: asyncpg.Connection) -> None:
    print("\nT2 COPY path idempotent (COPY itself does not support ON CONFLICT)")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(200)]
    await w._copy_upsert(rows)
    first = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    # Retry scenario: write the same batch again; must not error, must not add rows
    err = None
    try:
        await w._copy_upsert(rows)
    except Exception as exc:            # noqa: BLE001 -- we want to catch any exception here
        err = exc
    second = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("repeated batch raises no exception", err is None, repr(err))
    check("repeated batch row count unchanged (idempotent)", first == second == 200, f"{first} -> {second}")


async def t3_fallback(conn: asyncpg.Connection) -> None:
    print("\nT3 degradation chain: L1 fails -> L2 takes over")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(200)]

    original = w._copy_upsert

    async def broken(_rows):
        raise RuntimeError("simulated COPY path outage")

    w._copy_upsert = broken  # type: ignore[method-assign]
    try:
        await w._write_with_retry(rows)
    finally:
        w._copy_upsert = original  # type: ignore[method-assign]

    n = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("after L1 fully fails, L2 writes the data", n == 200, f"{n}/200")
    check("retries are counted (observable)", w.retries > 0, f"retries={w.retries}")


async def t4_isolation(conn: asyncpg.Connection) -> None:
    print("\nT4 bad-row isolation: one dirty row doesn't drag down the whole batch")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(100)]
    # insert one unwritable row in the middle (max_joint_temp is NOT NULL, set to None -> NotNullViolation)
    bad = list(rows[0])
    bad[1] = "humanoid-099"
    bad[6] = None                       # max_joint_temp = NULL
    bad[0] = datetime.fromtimestamp(1767225600.0 + 999, tz=timezone.utc)
    rows[50] = tuple(bad)

    before = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    await w._isolate_and_write(rows)
    after = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("good data preserved (99 rows)", after == 99, f"{after}/99 (1 bad row)")
    check("bad row counted as rejected", w.rejected == 1, f"rejected={w.rejected}")
    check("bad row truly not persisted", after == before + 99)


async def t5_throughput(conn: asyncpg.Connection) -> None:
    print("\nT5 write throughput redundancy vs peak")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    n = 5000
    rows = [w._to_row(make_telemetry(i)) for i in range(n)]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await w._copy_upsert(rows)
    dt = loop.time() - t0
    rps = n / dt
    check("COPY throughput > 1000 rows/s peak x 10", rps > 10000,
          f"{rps:,.0f} rows/s ({rps/1000:.0f}x redundancy)")


async def t6_schema_parity(conn: asyncpg.Connection) -> None:
    print("\nT6 ORM DDL column types match infra/init.sql")
    db_types = {
        r["column_name"]: r["data_type"]
        for r in await conn.fetch(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_name = 'robot_telemetry'"""
        )
    }
    mismatched = []
    for col in RobotTelemetry.__table__.columns:
        actual = db_types.get(col.name)
        if actual is None:
            mismatched.append(f"{col.name}: absent in DB")
            continue
        # JSONB shows as data_type='jsonb' in information_schema
        expected = col.type.__class__.__name__.lower()
        if expected == "jsonb" and actual != "jsonb":
            mismatched.append(f"{col.name}: ORM=jsonb DB={actual}")
        if expected == "json" and actual != "json":
            mismatched.append(f"{col.name}: ORM=json but DB={actual}")
    check("joint column ORM type == DB jsonb (no drift)", not mismatched, "; ".join(mismatched))
    check("COLUMNS count == table column count", len(COLUMNS) == len(db_types),
          f"{len(COLUMNS)} vs {len(db_types)}")


async def main() -> int:
    if not DSN:
        print("Skipped: TEST_DATABASE_URL not set (this test needs a real PostgreSQL)")
        return 0
    conn = await asyncpg.connect(DSN)
    try:
        print(f"== DB integration end-to-end verification ==\n{DSN}")
        for fn in (t1_copy_jsonb, t2_idempotent, t3_fallback, t4_isolation, t5_throughput, t6_schema_parity):
            await fn(conn)
    finally:
        await conn.close()
    print(f"\n== Result: {PASS} passed / {FAIL} failed ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
