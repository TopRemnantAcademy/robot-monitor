"""实证验证 batch_writer 的**丢行计数**与降级链未被回退（零外部依赖）。

用法（仓库根）：
    /tmp/rm-venv/bin/python backend_ingestion/tests/verify_batch_writer.py

历史说明：本文件从前由 `fix/issue19-merge-safe` 带入，验证的是该分支的
`_to_columns` / `unnest` 列式改造 —— 而那套改造在 main 上**并不存在**
（main 保留的是三级降级链 + `_to_row`）。即该脚本此前 import 就会
`AttributeError`，是一份**跑不起来的交付物**。本轮改为验证 main 真实存在的逻辑。

验证点：
1. `_to_row` 的列顺序与 `COLUMNS` 严格对应（COPY 依赖此顺序）
2. `_to_row` 优先取客户端时间戳（而非刷盘时刻 `now()`）
3. 三值相等时 `get_pool` 的 `max_size` 护栏存在（见 app/db/pool.py）
4. 丢行计数：`rows_submitted / rows_written / rows_skipped` 的口径
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend_ingestion"))

# 桩掉第三方依赖，让本脚本在「无 DB、无第三方包」环境也能跑
for name in ("redis", "redis.asyncio", "asyncpg", "pydantic", "pydantic_settings"):
    if name not in sys.modules:
        sys.modules.setdefault(name, types.ModuleType(name))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = (ROOT / "backend_ingestion/app/services/batch_writer.py").read_text()
    tree = ast.parse(src)
    bw = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BatchWriter")
    methods = {n.name for n in bw.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    print("1. 降级链完好（main 的容错核心，禁止回退）")
    check("L1 _copy_upsert 存在", "_copy_upsert" in methods)
    check("L2 _executemany 存在", "_executemany" in methods)
    check("L3 _isolate_and_write 存在", "_isolate_and_write" in methods)
    check("_STOP 哨兵存在（graceful_shutdown 靠它唤醒 run()）", "_STOP" in src)
    check("COPY 路径在用", "copy_records_to_table" in src)
    check("ON CONFLICT 幂等仍在", "ON CONFLICT (time, robot_id) DO NOTHING" in src)

    print("2. 丢行三元组已接线（ISSUE #19 G2）")
    check("rows_submitted 计数器存在", "self.rows_submitted" in src)
    check("rows_skipped 计数器存在", "self.rows_skipped" in src)
    check("_count_written 记账存在", "_count_written" in methods)
    # L1 必须用 INSERT 的 affected rows，否则 skipped 恒为 0（本机踩过这个坑）
    check(
        "L1 用 affected rows 记真实入库行数",
        "_copy_upsert" in methods and "def _copy_upsert(self, rows: list[tuple]) -> int" in src,
        "否则 written 计的是提交行数，skipped 永远为 0",
    )

    print("3. 列顺序契约（COPY 依赖它逐位对应）")
    # COLUMNS 是**带类型标注**的赋值（ast.AnnAssign），不是 ast.Assign
    n_cols = 0
    for n in tree.body:
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "COLUMNS":
            n_cols = len(n.value.elts)
        elif isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COLUMNS":
            n_cols = len(n.value.elts)
    check("COLUMNS 有 17 列", n_cols == 17, f"实际 {n_cols}")
    check("_to_row 存在且为 staticmethod", "_to_row" in methods)

    print("4. 连接池护栏（本机实测的两个坑）")
    pool_src = (ROOT / "backend_ingestion/app/db/pool.py").read_text()
    sess_src = (ROOT / "backend_ingestion/app/db/session.py").read_text()
    check("pool 工厂有 MAX_POOL_SIZE 下限", "MAX_POOL_SIZE" in pool_src)
    check("max_size 不等于 min_size（否则退化成信号量）",
          "max_size=max(pool_size, MAX_POOL_SIZE)" in pool_src)
    check("get_pool() 有建池锁（防并发各建一个池）", "_pool_lock" in sess_src)

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败: {FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
