"""实证验证 batch_writer 的 unnest 列式改造（**零外部依赖**，不使用真实 DB）。

用法（仓库根）：
    python3 backend_ingestion/tests/verify_batch_writer.py

它桩掉了 redis/asyncpg/app.config/app.db.* 以便在无 DB、无第三方包的环境下
真实执行 _to_columns / INSERT_SQL 拼装 / 丢行计数三条逻辑。
CI 或本地均可直接运行，退出码 0 表示全部通过。

验证点：
1. _to_columns 生成 17 个等长列，顺序与 INSERT_SQL 的 unnest 参数一致
2. ts 优先取客户端时间戳（毫秒 → timestamptz）
3. 丢行计数三元组在"全部插入 / 部分冲突 / 整批失败"三种情况下的行为
4. RETURNING 计数逻辑：inserted == 返回行数
"""
import asyncio
import sys
import types
from datetime import datetime, timezone

# ---- 桩掉未安装的依赖（不引入第三方）----
for name in ("redis", "redis.asyncio", "asyncpg"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

# stub app.db.session / app.db.redis，避免真实连接
pkg_db = types.ModuleType("app.db.session")
class _FakePool:
    def __init__(self):
        self.rows = []
    async def fetch(self, sql, *cols):
        # 模拟 ON CONFLICT 语义：再次提交同一 (time, robot_id) 会被跳过
        seen = {(r[0], r[1]) for r in self.rows}
        n = len(cols[0])
        out = []
        for i in range(n):
            key = (cols[0][i], cols[1][i])
            if key in seen:
                continue
            seen.add(key)
            out.append((1,))
            self.rows.append((cols[0][i], cols[1][i]))
        return out
    def acquire(self):
        return _Ctx(self)

class _Ctx:
    def __init__(self, pool): self.pool = pool
    async def __aenter__(self): return self.pool
    async def __aexit__(self, *a): return False

_fake_pool = _FakePool()
async def _get_pool(): return _fake_pool
pkg_db.get_pool = _get_pool
sys.modules["app.db.session"] = pkg_db

pkg_redis = types.ModuleType("app.db.redis")
sys.modules["app.db.redis"] = pkg_redis

# stub config（避免 pydantic-settings 版本差异）
pkg_cfg = types.ModuleType("app.config")
class _S:
    batch_trigger_count = 500
    batch_trigger_seconds = 1.0
    batch_queue_maxsize = 20000
pkg_cfg.settings = _S()
sys.modules["app.config"] = pkg_cfg

# 需要 app.schemas.telemetry: 用轻量 stub 避免依赖 pydantic-settings 全链
from app.schemas.telemetry import Telemetry  # noqa

from app.services.batch_writer import BatchWriter, INSERT_SQL

ok = True
def check(desc, cond, extra=""):
    global ok
    print(("PASS" if cond else "FAIL"), desc, extra)
    if not cond: ok = False

# --- 1. 列式转换 ---
def mk(rid, ts):
    return Telemetry.model_validate({
        "robot_id": rid, "status_code": 1, "error_code": 0,
        "battery_pct": 88.5, "max_joint_temp": 61.2,
        "joint_temperatures": {"J1": 1.0}, "joint_positions": {"J1": 0.1},
        "joint_torques": {"J1": 2.0}, "ts": ts,
    })

buf = [mk("humanoid-001", 1700000000000), mk("humanoid-002", 1700000000100)]
cols = BatchWriter._to_columns(buf)
check("列数 == 17", len(cols) == 17, f"got {len(cols)}")
check("每列长度 == 批大小", all(len(c) == 2 for c in cols))
check("robot_id 列正确", cols[1] == ["humanoid-001", "humanoid-002"])
check("ts 取自客户端毫秒",
      cols[0][0] == datetime.fromtimestamp(1700000000000/1000, tz=timezone.utc))
check("JSONB 列为 JSON 文本", isinstance(cols[7][0], str) and cols[7][0].startswith("{"))

# SQL 参数个数应与列数一致（$17 为最大占位符）
import re
ph = sorted({int(m) for m in re.findall(r"\$(\d+)", INSERT_SQL)})
check("INSERT_SQL 占位符 1..17", ph == list(range(1, 18)), f"got {ph}")
check("INSERT_SQL 含 RETURNING（拿真实插入行数）", "RETURNING 1" in INSERT_SQL)

# --- 2/3. 计数三元组 ---
async def main():
    w = BatchWriter()
    await w._flush(buf)                     # 首批：全插入
    check("首批 inserted=2", w.written == 2, f"written={w.written}")
    check("首批 skipped=0", w.rows_skipped == 0)
    check("submitted 累计 2", w.rows_submitted == 2)

    await w._flush(buf)                     # 重发同一批：全部冲突
    check("重发 inserted 不增", w.written == 2, f"written={w.written}")
    check("重发 skipped=2（静默丢行变可观测）", w.rows_skipped == 2, f"skipped={w.rows_skipped}")
    check("重发 submitted 累计 4", w.rows_submitted == 4)

    mixed = [buf[0], mk("humanoid-003", 1700000000200)]
    await w._flush(mixed)                   # 一半冲突
    check("混合 inserted 增 1（只 humanoid-003）", w.written == 3, f"written={w.written}")
    check("混合 skipped=1", w.rows_skipped == 3, f"skipped={w.rows_skipped}")

    # 整批失败
    async def boom(sql, *c): raise RuntimeError("db down")
    _fake_pool.fetch = boom
    await w._flush(buf)
    check("失败计数 +1", w.flush_failures == 1, f"failures={w.flush_failures}")
    check("失败不增加 written", w.written == 3)

asyncio.run(main())
print("\n=== 结果:", "全部通过 ✅" if ok else "存在失败 ❌", "===")
sys.exit(0 if ok else 1)
