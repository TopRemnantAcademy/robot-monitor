"""ISSUE #96 验收 harness（真实 PostgreSQL，非 mock）。

⚠️ 本文件**不是** pytest 用例（与同目录 `verify_batch_writer.py` /
`verify_db_integration.py` 一致：脚本不是 `test_*.py`，pytest 不会收集它）。
它是**按需运行**的独立脚本，退出码 0 = 全部通过。

判据与本单「验收」逐条对应：
  A. 跑遥测后 robot_metadata.is_online / last_seen 有真实值（非全 NULL / 全 false）
  B. 停发 > state_ttl_seconds 的机器人 is_online 翻 false，其余不连坐
  C. 写放大：1000 条遥测（=1s 的 1000 msg/s 峰值）只产生 1 条 UPDATE / 100 个参数行
  D. offline 规则在真实列上的行为（online / offline / 回落）
  E. 值未变不写（IS DISTINCT FROM 幂等）

跑法：
    # 需要一个可写的 PostgreSQL（hypertable/连续聚合不需要）
    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
    export JWT_SECRET=test-only-secret
    cd backend_ingestion && python -m tests.verify_metadata_heartbeat

未设 `TEST_DATABASE_URL` 时打印「跳过」并退出 0 —— 所以它**永远不该**被当成
CI 门禁，只作为有 DB 时的人工复核手段。

前置：目标库已执行 `infra/init.sql` 的 `robot_metadata` 部分（100 台播种）。
`src` 类断言（列定义未被改动）由 `backend_ingestion/tests/test_metadata_writer.py`
覆盖，本文件只做**行为**验证。
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
    print("【SKIP】未设 TEST_DATABASE_URL —— 本脚本需要真实 PostgreSQL，跳过。")
    sys.exit(0)

import asyncpg  # noqa: E402

from app.services.metadata_writer import (  # noqa: E402
    MARK_OFFLINE_SQL,
    MetadataWriter,
)

PASS = FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    PASS += 1 if ok else 0
    FAIL += 0 if ok else 1


class SpyPool:
    """包一层真池，记录所有下发语句（用于证明写放大）。"""

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
    """按文件路径加载 alert_engine 的规则（两个模块都有顶层包 `app`，不能直接 import）。"""
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

    # ================= A + C：1000 msg/s 峰值 =================
    await pool.execute("UPDATE robot_metadata SET is_online=FALSE, last_seen=NULL")
    w = MetadataWriter()
    for _ in range(10):                      # 100 台 × 10 条 = 1000 条 = 1 秒峰值
        for i in range(1, 101):
            w.put(f"humanoid-{i:03d}", int(time.time() * 1000))
    t0 = time.perf_counter()
    await w.flush()
    dt = (time.perf_counter() - t0) * 1000

    ups = [s for s in spy.stmts if "unnest" in s[1]]
    chk("C1 1000 条遥测 → 恰好 1 条 UPDATE 语句", len(ups) == 1, f"实际 {len(ups)} 条")
    chk("C2 该语句只带 100 个 robot_id（按主键折叠）", len(ups[0][2][0]) == 100)
    chk("C3 折叠掉 900 条重复（1000 → 100）", w.stats["deduped"] == 900,
        f"deduped={w.stats['deduped']}")
    chk("C4 单词往返耗时 << 1s 刷新周期", dt < 500, f"{dt:.1f} ms")
    r = await pool.fetchrow(
        "SELECT count(*) c, sum(CASE WHEN is_online THEN 1 ELSE 0 END) onl, count(last_seen) ls"
        " FROM robot_metadata")
    chk("A1 100 台全 is_online=true（非全 false）", r["onl"] == 100, f"online={r['onl']}")
    chk("A2 last_seen 全非 NULL（非全 NULL）", r["ls"] == 100, f"last_seen 非空 {r['ls']}/100")

    # ================= B：停发一台 > state_ttl_seconds =================
    await pool.execute("UPDATE robot_metadata SET is_online=FALSE, last_seen=NULL")
    w2 = MetadataWriter()
    for i in range(2, 101):                  # 001 停发
        w2.put(f"humanoid-{i:03d}")
    await w2.flush()
    # 把 001 的 last_seen 推到 11s 前，等价于「已停发 11s」（不必真等 10 秒）
    await pool.execute("UPDATE robot_metadata SET last_seen=NOW()-INTERVAL '11 seconds'"
                       " WHERE robot_id='humanoid-001'")
    await w2.flush()
    row = await pool.fetchrow("SELECT is_online FROM robot_metadata WHERE robot_id='humanoid-001'")
    onl = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("B1 停发 >10s 的机器人 is_online 翻 false", row["is_online"] is False,
        f"001={row['is_online']}")
    chk("B2 其余 99 台仍 true（无连坐误报）", onl == 99, f"online={onl}")

    # ================= D：offline 规则读真实列 =================
    mod = _load_rule(os.path.join(ROOT, "alert_engine/app/rules/offline.py"))
    rule = mod.OfflineRule()
    real = {r["robot_id"]: r["is_online"] for r in
            await pool.fetch("SELECT robot_id, is_online FROM robot_metadata")}
    old = time.time() - 11
    chk("D1 真实列为 false → offline 告警",
        rule.evaluate({"robot_id": "humanoid-001", "is_online": real["humanoid-001"],
                       "last_seen": old}, []) is not None)
    chk("D2 真实列为 true → 不报",
        rule.evaluate({"robot_id": "humanoid-002", "is_online": real["humanoid-002"],
                       "last_seen": time.time()}, []) is None)
    chk("D3 列不可用（None）但 last_seen 很新 → 回落不误报",
        rule.evaluate({"robot_id": "humanoid-003", "is_online": None,
                       "last_seen": time.time()}, []) is None)
    chk("D4 判据单源（刚收到遥测但列判离线 → 仍报）",
        rule.evaluate({"robot_id": "humanoid-001", "is_online": False,
                       "last_seen": time.time() - 3}, []) is not None)
    chk("D5 列不可用 + last_seen 超时 → 回落报出（迁移期安全网）",
        rule.evaluate({"robot_id": "humanoid-004", "is_online": None,
                       "last_seen": old}, []) is not None)

    # ================= E：幂等 =================
    before = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    w3 = MetadataWriter()
    await w3.flush()                         # 空窗口（无新遥测、无新离线）
    after = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("E1 空刷新不改变任何在线状态", before == after, f"{before} → {after}")
    chk("E2 空刷新的离线扫描命中 0 行", w3.stats["offline_marks"] == 0)

    # ================= F：真实 MARK_OFFLINE_SQL 定点验证 =================
    await pool.execute("UPDATE robot_metadata SET is_online=TRUE, last_seen=NOW()")
    await pool.execute("UPDATE robot_metadata SET last_seen=NOW()-INTERVAL '11 seconds'"
                       " WHERE robot_id IN ('humanoid-001','humanoid-002')")
    rows = await pool.fetch(MARK_OFFLINE_SQL, float(settings.state_ttl_seconds))
    ids = sorted(x["robot_id"] for x in rows)
    chk("F1 定向置 false 只命中真正超时的机器人", ids == ["humanoid-001", "humanoid-002"], f"{ids}")
    n = await pool.fetchval("SELECT count(*) FROM robot_metadata WHERE is_online")
    chk("F2 未超时的 98 台保持 true（WHERE is_online=TRUE 守卫有效）", n == 98, f"online={n}")

    await pool.close()
    print(f"\nPASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
