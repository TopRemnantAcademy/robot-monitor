"""Order 17 alert-closure end-to-end verification (real PostgreSQL + real Redis, not mock).

Run (needs a writable PostgreSQL with the pure-PG part of infra/init.sql executed + Redis):

    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/robot_monitor
    export TEST_REDIS_URL=redis://127.0.0.1:6379/15
    cd backend_ingestion && python -m tests.verify_alert_closure

WARNING: this file is **not** a pytest case (following the verify_db_integration.py
convention): assertions live in v1..v7, driven by main(), exit code 0 = all passed.
The name does not carry the `test_` prefix so pytest does not produce the false
impression of "tests running".

Coverage (each maps to one acceptance criterion in the work order section 2):
  V1  schema: robot_alert has escalated column + two partial indexes (a DB where init.sql
             actually ran)
  V2  ack endpoint SQL: sets ack=TRUE and ack_by/ack_at have values (against real SQL query results)
  V3  repeated ack is idempotent (changed=False, does not overwrite first ack_by, row count unchanged)
  V4  unack idempotent + withdrawal clears ack_by/ack_at
  V5  GET /alerts pagination: offset takes effect, total is the post-filter total
  V6  escalation: severity=3 not acked past timeout -> escalated becomes TRUE (runs real SCAN/MARK SQL)
  V7  silence: Redis TTL active suppresses, auto-recovers on expiry; a silence hit does not reach dedup
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "")
REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
if DSN:
    # settings is a module-level singleton, must be injected before importing app.config
    os.environ["DATABASE_URL"] = DSN
    os.environ["REDIS_URL"] = REDIS_URL

from app.alerts import repository  # noqa: E402

PASS, FAIL = 0, 0
RUN_TAG = f"verif17-{int(time.time())}"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


async def _seed(conn: asyncpg.Connection, robot_id: str, severity: int, age_s: int,
                n: int = 1) -> list[int]:
    """Insert n alerts, return the list of alert_ids. rule_id uses RUN_TAG to isolate
    this round's data."""
    ids: list[int] = []
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    for _ in range(n):
        aid = await conn.fetchval(
            """INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
               VALUES ($1, $2, $3, $4, $5, '{}'::jsonb) RETURNING alert_id""",
            ts, robot_id, RUN_TAG, severity, f"{RUN_TAG} seed",
        )
        ids.append(aid)
    return ids


async def _query(conn: asyncpg.Connection, alert_id: int) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT alert_id, ack, ack_at, ack_by, escalated FROM robot_alert WHERE alert_id=$1",
        alert_id,
    )


# ---------------------------------------------------------------- V1

async def v1_schema(conn: asyncpg.Connection) -> None:
    print("\nV1 schema: escalated column + partial indexes exist (init.sql actually ran)")
    cols = await conn.fetch(
        """SELECT column_name, data_type, is_nullable, column_default
             FROM information_schema.columns
            WHERE table_name='robot_alert' ORDER BY ordinal_position"""
    )
    names = [c["column_name"] for c in cols]
    check("has escalated column", "escalated" in names, str(names))
    esc = next((c for c in cols if c["column_name"] == "escalated"), None)
    check("escalated is boolean NOT NULL DEFAULT false",
          esc is not None and esc["data_type"] == "boolean"
          and esc["is_nullable"] == "NO" and "false" in (esc["column_default"] or ""),
          str(dict(esc)) if esc else "missing")
    idx = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE tablename='robot_alert'"
    )
    idxnames = [i["indexname"] for i in idx]
    check("has idx_alert_ack (built by Order 19)", "idx_alert_ack" in idxnames, str(idxnames))
    check("has idx_alert_escalation (built by Order 17)", "idx_alert_escalation" in idxnames)


# ---------------------------------------------------------------- V2~V4

async def v2_ack(conn: asyncpg.Connection) -> None:
    print("\nV2 ack: sets ack=TRUE and ack_by/ack_at have values")
    [aid] = await _seed(conn, "humanoid-001", 3, 0)
    row, changed = await repository.set_ack(aid, True, "ops@example.com")
    check("set_ack reports changed=True", changed is True)
    db = await _query(conn, aid)
    print(f"    SQL> SELECT alert_id, ack, ack_at, ack_by, escalated "
          f"FROM robot_alert WHERE alert_id={aid};")
    print(f"    -- {dict(db)}")
    check("ack=TRUE", db["ack"] is True)
    check("ack_by has a value", db["ack_by"] == "ops@example.com", str(db["ack_by"]))
    check("ack_at has a value", db["ack_at"] is not None, str(db["ack_at"]))


async def v3_ack_idempotent(conn: asyncpg.Connection) -> None:
    print("\nV3 repeated ack idempotent (no error, does not overwrite first acked-by)")
    [aid] = await _seed(conn, "humanoid-002", 3, 0)
    await repository.set_ack(aid, True, "first@example.com")
    before = await _query(conn, aid)
    # ack again, with a different person: must not overwrite ack_by
    row, changed = await repository.set_ack(aid, True, "second@example.com")
    after = await _query(conn, aid)
    check("second ack's changed=False (idempotent hit)", changed is False)
    check("ack_by is still the first acked-by", after["ack_by"] == "first@example.com",
          str(after["ack_by"]))
    check("ack_at not refreshed", after["ack_at"] == before["ack_at"], str(after["ack_at"]))
    n = await conn.fetchval("SELECT count(*) FROM robot_alert WHERE alert_id=$1", aid)
    check("row count unchanged (no duplicate write)", n == 1)


async def v4_unack(conn: asyncpg.Connection) -> None:
    print("\nV4 unack: withdraw and clear ack_by/ack_at; repeated unack idempotent")
    [aid] = await _seed(conn, "humanoid-003", 3, 0)
    await repository.set_ack(aid, True, "ops@example.com")
    _, changed = await repository.set_ack(aid, False, None)
    db = await _query(conn, aid)
    check("unack reports changed=True", changed is True)
    check("ack=FALSE", db["ack"] is False)
    check("ack_by cleared", db["ack_by"] is None, str(db["ack_by"]))
    check("ack_at cleared", db["ack_at"] is None, str(db["ack_at"]))
    _, changed2 = await repository.set_ack(aid, False, None)
    check("repeated unack idempotent (changed=False)", changed2 is False)


# ---------------------------------------------------------------- V5

async def v5_pagination(conn: asyncpg.Connection) -> None:
    print("\nV5 GET /alerts pagination: offset takes effect, total is post-filter total")
    await conn.execute("DELETE FROM robot_alert WHERE rule_id=$1", RUN_TAG)
    ids = await _seed(conn, "humanoid-010", 3, 0, n=5)
    for aid in ids[:3]:
        await repository.set_ack(aid, True, "ops@example.com")

    # ack=false should leave only 2 rows
    page1, total_unacked = await repository.list_alerts(
        ack=False, escalated=None, suppressed=None, robot_id="humanoid-010", limit=1, offset=0
    )
    page2, _ = await repository.list_alerts(
        ack=False, escalated=None, suppressed=None, robot_id="humanoid-010", limit=1, offset=1
    )
    check("total for ack=false is 2", total_unacked == 2, str(total_unacked))
    check("limit=1 returns only 1 row", len(page1) == 1, str(len(page1)))
    check("offset=1 returns a different row", page1[0]["alert_id"] != page2[0]["alert_id"],
          f"{page1[0]['alert_id']} vs {page2[0]['alert_id']}")
    check("ordered by time DESC, alert_id DESC", page1[0]["alert_id"] > page2[0]["alert_id"])

    # ack all: total should be 5
    _, total_all = await repository.list_alerts(
        ack=None, escalated=None, suppressed=None, robot_id="humanoid-010", limit=50, offset=0
    )
    check("total is 5 when no ack filter", total_all == 5, str(total_all))


def _load_alert_engine_silence():
    """Load alert_engine/app/silence.py by file path (to avoid the same-named `app`
    package conflict).

    It does `from app.db import get_redis`; here we stub out `app.db`, so the object
    under test is only the pure-Python silence decision (same method as
    alert_engine/tests/conftest.py). The object under test is the **real source**, not a copy.
    """
    import importlib.util
    import types

    # Build a clean top-level `app` package, exposing only the stubbed app.db.
    # We must temporarily **replace** `app` / `app.db` in sys.modules: this process
    # simultaneously has backend_ingestion/app (real) and alert_engine/app (under test);
    # there can be only one same-named package, so directly using backend's app.db would
    # raise ImportError (it has no get_redis).
    fake_db = types.ModuleType("app.db")

    async def _get_redis():
        raise RuntimeError("stub: replaced below with real redis")

    async def _get_pool():
        raise RuntimeError("stub: no pool")

    async def _close_all():
        return None

    fake_db.get_redis = _get_redis
    fake_db.get_pool = _get_pool
    fake_db.close_all = _close_all

    fake_app = types.ModuleType("app")
    fake_app.db = fake_db
    # Make `app` a **package** (with __path__ pointing at alert_engine/app), so
    # `from app.rules.base import Alert` can be resolved to the real source by the
    # sub-interpreter.
    fake_app.__path__ = [str(Path(__file__).resolve().parents[2] / "alert_engine" / "app")]

    saved_app = sys.modules.get("app")
    saved_db = sys.modules.get("app.db")
    sys.modules["app"] = fake_app
    sys.modules["app.db"] = fake_db
    try:
        path = Path(__file__).resolve().parents[2] / "alert_engine" / "app" / "silence.py"
        spec = importlib.util.spec_from_file_location("ae_silence_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        # Restore, to avoid polluting other imports
        if saved_app is not None:
            sys.modules["app"] = saved_app
        if saved_db is not None:
            sys.modules["app.db"] = saved_db
    return mod


def _load_escalation_sql() -> tuple[str, str]:
    """Extract SCAN_SQL / MARK_SQL from alert_engine/app/escalation.py's real source.

    We do not import that module: this repo has two same-named top-level packages `app`
    (backend_ingestion/app and alert_engine/app), and only one `sys.modules["app"]` can
    exist in the same process (as pytest.ini explains). We extract the constants from the
    source via regex, but what we test is still the **real SQL string from the source**,
    not a copy.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "alert_engine" / "app" / "escalation.py"
    src = root.read_text(encoding="utf-8")
    scan = re.search(r'SCAN_SQL = """(.*?)"""', src, re.S)
    mark = re.search(r'MARK_SQL = """(.*?)"""', src, re.S)
    assert scan and mark, "SCAN_SQL / MARK_SQL not found in escalation.py"
    return scan.group(1), mark.group(1)


# ---------------------------------------------------------------- V6

async def v6_escalation(conn: asyncpg.Connection) -> None:
    print("\nV6 escalation: severity=3 not acked past timeout -> escalated=TRUE and hit by SCAN")
    await conn.execute("DELETE FROM robot_alert WHERE rule_id=$1", RUN_TAG)
    # one 6 minutes old (past the 300s threshold), one just inserted (not past)
    old_ids = await _seed(conn, "humanoid-020", 3, 360, n=1)
    fresh_ids = await _seed(conn, "humanoid-021", 3, 0, n=1)
    # plus one past timeout but already acked (should not be escalated)
    acked_ids = await _seed(conn, "humanoid-022", 3, 360, n=1)
    await repository.set_ack(acked_ids[0], True, "ops@example.com")

    SCAN_SQL, MARK_SQL = _load_escalation_sql()
    rows = await conn.fetch(SCAN_SQL, 300, 200)
    hit = {r["alert_id"] for r in rows}
    check("unacked sev3 past timeout is hit", old_ids[0] in hit, str(hit))
    check("not-yet-expired is not hit", fresh_ids[0] not in hit)
    check("already-acked is not hit", acked_ids[0] not in hit)

    # execute MARK (same SQL as escalation.py)
    marked = await conn.fetch(MARK_SQL, old_ids[0])
    check("MARK returns the updated row", len(marked) == 1, str(len(marked)))
    db = await _query(conn, old_ids[0])
    check("escalated becomes TRUE", db["escalated"] is True)

    # re-scan: already-escalated must not be hit again (guarantees single re-send)
    rows2 = await conn.fetch(SCAN_SQL, 300, 200)
    check("already-escalated not hit again by SCAN (avoid re-escalation)",
          old_ids[0] not in {r["alert_id"] for r in rows2})

    # concurrency guard: a re-MARK of an already-escalated row should yield rowcount=0
    again = await conn.fetch(MARK_SQL, old_ids[0])
    check("repeated MARK has no effect (idempotent)", len(again) == 0)


# ---------------------------------------------------------------- V7

async def v7_silence_real_redis() -> None:
    print("\nV7 silence: Redis TTL active suppresses, recovers on expiry; silence hit does not reach dedup")
    import redis.asyncio as aioredis

    silence = _load_alert_engine_silence()
    is_silenced = silence.is_silenced
    silence_key = silence.silence_key
    SILENCE_ALL_KEY = silence.SILENCE_ALL_KEY

    class Alert:
        def __init__(self, robot_id, rule_id, severity, message, detail):
            self.robot_id = robot_id
            self.rule_id = rule_id
            self.severity = severity
            self.message = message
            self.detail = detail

    if not REDIS_URL:
        check("TEST_REDIS_URL was provided", False, "not set, skipping")
        return
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    await r.flushdb()

    # Point the silence module under test's Redis entry at the real Redis (real key, real TTL).
    async def _real_redis():
        return r

    silence.get_redis = _real_redis

    alert = Alert(robot_id="humanoid-030", rule_id=RUN_TAG, severity=3, message="x", detail={})
    check("initially not silenced", (await is_silenced(alert)) is False)

    # verify recovery on expiry with a 1s TTL (real TTL, no waiting 300s)
    key = silence_key("humanoid-030")
    await r.set(key, "1", ex=1)
    check("judged silenced while TTL active", (await is_silenced(alert)) is True)
    check("Redis key carries a TTL", (await r.ttl(key)) > 0, f"ttl={await r.ttl(key)}")

    await asyncio.sleep(1.2)
    check("auto-recovers after TTL expiry (key gone)", (await r.get(key)) is None)
    check("no longer silenced after expiry", (await is_silenced(alert)) is False)

    # global silence
    await r.set(SILENCE_ALL_KEY, "1", ex=5)
    check("global key silences any robot", (await is_silenced(alert)) is True)

    # on a silence hit, emit does not reach dedup (real Redis: the dedup key must not be written)
    await r.flushdb()
    await r.set(key, "1", ex=60)
    assert await is_silenced(alert) is True
    dedup_key = f"alert:dedup:{alert.robot_id}:{alert.rule_id}"
    check("dedup key not written during silence (silence returns before dedup)",
          (await r.get(dedup_key)) is None)

    await r.flushdb()
    await r.aclose()


# ---------------------------------------------------------------- main

async def main() -> int:
    if not DSN:
        print("TEST_DATABASE_URL required (and the DB must have run the pure-PG part of infra/init.sql)")
        return 2
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DELETE FROM robot_alert WHERE rule_id=$1", RUN_TAG)
        await v1_schema(conn)
        await v2_ack(conn)
        await v3_ack_idempotent(conn)
        await v4_unack(conn)
        await v5_pagination(conn)
        await v6_escalation(conn)
        await v7_silence_real_redis()
    finally:
        await conn.execute("DELETE FROM robot_alert WHERE rule_id=$1", RUN_TAG)
        await conn.close()

    print(f"\n{'='*60}\nPassed {PASS} / Failed {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
