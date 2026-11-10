"""ISSUE #96 acceptance harness (real PostgreSQL, not mock).

WARNING: this file is **not** a pytest case (consistent with the same-directory
`verify_batch_writer.py` / `verify_db_integration.py`: the script is not `test_*.py`,
so pytest will not collect it). It is a **run-on-demand** standalone script; exit code
0 = all passed.

The criteria map line-by-line to this issue's "acceptance":
  A. After running telemetry, robot_metadata.is_online / last_seen have real values
     (not all NULL / all false)
  B. A robot that stopped sending for > state_ttl_seconds flips is_online to false;
     the rest are not dragged along
  C. Write amplification: 1000 telemetry records (= 1s of 1000 msg/s peak) yield only
     1 UPDATE statement / 100 parameter rows
  D. The offline rule's behavior on real columns (online / offline / fallback)
  E. Unchanged values are not written (IS DISTINCT FROM idempotency)

Run:
    # needs a writable PostgreSQL (hypertable / continuous aggregates not required)
    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
    export JWT_SECRET=test-only-secret
    cd backend_ingestion && python -m tests.verify_metadata_heartbeat

When TEST_DATABASE_URL is unset it prints "skipped" and exits 0 -- so it should
**never** be treated as a CI gate; it is only a manual recheck when a DB is available.

Prerequisite: the target DB has run the `robot_metadata` portion of `infra/init.sql`
(100 robots seeded). The `src`-class assertions (column definitions unchanged) are
covered by `backend_ingestion/tests/test_metadata_writer.py`; this file only does
**behavior** verification.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/robot_monitor"),
)
os.environ.setdefault("JWT_SECRET", "test-only-secret")
sys.path.insert(0, os.path.join(ROOT, "backend_ingestion"))

DSN = os.environ.get("TEST_DATABASE_URL", "")
if not DSN:
    print("[SKIP] TEST_DATABASE_URL not set -- this script needs a real PostgreSQL; skipping.")
    sys.exit(0)

import asyncpg  # noqa: E402

from app.services.metadata_writer import (  # noqa: E402
    MARK_OFFLINE_SQL,
    MetadataWriter,
)

PASS = FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1


class SpyPool:
    """Wraps a real pool and records every issued statement (to prove write amplification)."""

    def __init__(self, pool):
        self._pool, self.stmts = pool, []

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                self._conn = await outer._pool.acquire()
                return _SpyConn(self._conn, outer.stmts)

            async def __aexit__(self, *exc):
                await outer._pool.release(self._conn)
                return False

        return _Ctx()


class _SpyConn:
    def __init__(self, conn, stmts):
        self._c, self._s = conn, stmts

    async def execute(self, sql, *args):
        self._s.append(("execute", sql, args))
        return await self._c.execute(sql, *args)

    async def fetch(self, sql, *args):
        self._s.append(("fetch", sql, args))
        return await self._c.fetch(sql, *args)


def _load_rule(path: str):
    """Load alert_engine's rule by file path (both modules have a top-level `app` package,
    so a direct import is impossible)."""
    base = types.ModuleType("app.rules.base")

    class Rule:
        rule_id = ""
        severity = dedup_window_s = 0

    class Alert:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    base.Rule, base.Alert = Rule, Alert
    conf = types.ModuleType("app.config")

    class _S:
        offline_after_s, offline_dedup_s = 10.0, 30
        offline_use_is_online = True

    conf.settings = _S()
    sys.modules["app.rules.base"], sys.modules["app.config"] = base, conf
    spec = importlib.util.spec_from_file_location("app.rules.offline", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["app.rules.offline"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    spy = SpyPool(pool)

    import app.services.metadata_writer as mw
    mw.get_pool = lambda: asyncio.sleep(0, result=spy)

    from app.config import settings

    # ================= A + C: 1000 msg/s peak =================
    await pool.execute("UPDATE robot_metadata SET is_online=FALSE, last_seen=NULL")
    w = MetadataWriter()
    for _ in range(10):                      # 100 robots x 10 records = 1000 records = 1s peak
        for i in range(1, 101):
            w.put(f"humanoid-{i:03d}", int(time.time() * 1000))
    t0 = time.perf_counter()
    await w.flush()
    dt = (time.perf_counter() - t0) * 1000

    ups = [s for s in spy.stmts if "unnest" in s[1]]
    chk("C1 1000 telemetry records -> exactly 1 UPDATE statement", len(ups) == 1, f"actual {len(ups)}")
    chk("C2 that statement carries only 100 robot_ids (folded by primary key)", len(ups[0][2][0]) == 100)
    chk("C3 900 duplicates folded away (1000 -> 100)", w.stats["deduped"] == 900,
        f"deduped={w.stats['deduped']}")
    chk("C4 single round-trip time << 1s flush period", dt < 500, f"{dt:.1f} ms")
    r = await pool.fetchrow(
        "SELECT count(*) c, sum(CASE WHEN is_online THEN 1 ELSE 0 END) onl, count(last_seen) ls"
        " FROM robot_metadata")
    chk("A1 all 100 robots is_online=true (not all false)", r["onl"] == 100, f"online={r['onl']}")
    chk("A2 last_seen all non-NULL (not all NULL)", r["ls"] == 100, f"last_seen non-null {r['ls']}/100")

    # ================= B: one robot stopped sending > state_ttl_seconds =================
    await pool.execute("UPDATE robot_metadata SET is_online=FALSE, last_seen=NULL")
    w2 = MetadataWriter()
    for i in range(2, 101):                  # 001 stopped sending
        w2.put(f"humanoid-{i:03d}")
    await w2.flush()
    # push 001's last_seen to 11s ago, equivalent to "stopped sending 11s" (no need to actually wait 10s)
    await pool.execute("UPDATE robot_metadata SET last_seen=NOW()-INTERVAL '11 seconds'"
                       " WHERE robot_id='humanoid-001'")
    await w2.flush()
    row = await pool.fetchrow("SELECT is_online FROM robot_metadata WHERE robot_id='humanoid-001'")
    onl = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("B1 robot stopped >10s flips is_online to false", row["is_online"] is False,
        f"001={row['is_online']}")
    chk("B2 the other 99 stay true (no drag-along false positive)", onl == 99, f"online={onl}")

    # ================= D: offline rule reads real columns =================
    mod = _load_rule(os.path.join(ROOT, "alert_engine/app/rules/offline.py"))
    rule = mod.OfflineRule()
    real = {r["robot_id"]: r["is_online"] for r in
            await pool.fetch("SELECT robot_id, is_online FROM robot_metadata")}
    old = time.time() - 11
    chk("D1 real column false -> offline alert",
        rule.evaluate({"robot_id": "humanoid-001", "is_online": real["humanoid-001"],
                       "last_seen": old}, []) is not None)
    chk("D2 real column true -> no alert",
        rule.evaluate({"robot_id": "humanoid-002", "is_online": real["humanoid-002"],
                       "last_seen": time.time()}, []) is None)
    chk("D3 column unavailable (None) but last_seen very fresh -> fallback, no false positive",
        rule.evaluate({"robot_id": "humanoid-003", "is_online": None,
                       "last_seen": time.time()}, []) is None)
    chk("D4 single-source criterion (just received telemetry but column says offline -> still alerts)",
        rule.evaluate({"robot_id": "humanoid-001", "is_online": False,
                       "last_seen": time.time() - 3}, []) is not None)
    chk("D5 column unavailable + last_seen expired -> fallback alerts (migration-period safety net)",
        rule.evaluate({"robot_id": "humanoid-004", "is_online": None,
                       "last_seen": old}, []) is not None)

    # ================= E: idempotency =================
    before = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    w3 = MetadataWriter()
    await w3.flush()                         # empty window (no new telemetry, no new offline)
    after = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("E1 empty flush changes no online state", before == after, f"{before} -> {after}")
    chk("E2 empty flush's offline scan hits 0 rows", w3.stats["offline_marks"] == 0)

    # ================= F: pinpoint verification of real MARK_OFFLINE_SQL =================
    await pool.execute("UPDATE robot_metadata SET is_online=TRUE, last_seen=NOW()")
    await pool.execute("UPDATE robot_metadata SET last_seen=NOW()-INTERVAL '11 seconds'"
                       " WHERE robot_id IN ('humanoid-001','humanoid-002')")
    rows = await pool.fetch(MARK_OFFLINE_SQL, float(settings.state_ttl_seconds))
    ids = sorted(x["robot_id"] for x in rows)
    chk("F1 targeted set-to-false only hits robots truly past timeout",
        ids == ["humanoid-001", "humanoid-002"], f"{ids}")
    n = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("F2 the 98 not-yet-expired stay true (WHERE is_online=TRUE guard effective)", n == 98, f"online={n}")

    await pool.close()
    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
