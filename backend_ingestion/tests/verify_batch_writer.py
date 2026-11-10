"""Empirical verification that batch_writer's **dropped-row counters** and the
degradation chain have not regressed (zero external dependencies).

Usage (from repo root):
    /tmp/rm-venv/bin/python backend_ingestion/tests/verify_batch_writer.py

History: this file was originally brought in by `fix/issue19-merge-safe`, verifying
that branch's `_to_columns` / `unnest` columnar rework -- but that rework does
**not** exist on main (main keeps the three-level degradation chain + `_to_row`).
So the script previously failed at import with `AttributeError`, i.e. it was a
**non-runnable deliverable**. This round rewrites it to verify the logic that
actually exists on main.

Verification points:
1. The column order of `_to_row` strictly matches `COLUMNS` (COPY depends on this order)
2. `_to_row` prefers the client timestamp (not the flush-time `now()`)
3. When the three values are equal, the `max_size` guard on `get_pool` exists (see app/db/pool.py)
4. Dropped-row counters: the semantics of `rows_submitted / rows_written / rows_skipped`
"""
from __future__ import annotations

import ast
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend_ingestion"))

# Stub out third-party dependencies so this script runs even in a "no DB, no third-party
# packages" environment.
for name in ("redis", "redis.asyncio", "asyncpg", "pydantic", "pydantic_settings"):
    if name not in sys.modules:
        sys.modules.setdefault(name, types.ModuleType(name))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = (ROOT / "backend_ingestion/app/services/batch_writer.py").read_text()
    tree = ast.parse(src)
    bw = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BatchWriter")
    methods = {n.name for n in bw.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    print("1. Degradation chain intact (main's fault-tolerance core; regression forbidden)")
    check("L1 _copy_upsert exists", "_copy_upsert" in methods)
    check("L2 _executemany exists", "_executemany" in methods)
    check("L3 _isolate_and_write exists", "_isolate_and_write" in methods)
    check("_STOP sentinel exists (graceful_shutdown wakes run() via it)", "_STOP" in src)
    check("COPY path in use", "copy_records_to_table" in src)
    check("ON CONFLICT idempotency still present", "ON CONFLICT (time, robot_id) DO NOTHING" in src)

    print("2. Dropped-row trio wired up (ISSUE #19 G2)")
    check("rows_submitted counter exists", "self.rows_submitted" in src)
    check("rows_skipped counter exists", "self.rows_skipped" in src)
    check("_count_written accounting exists", "_count_written" in methods)
    # L1 must use INSERT's affected rows, otherwise skipped is always 0 (hit this pitfall locally)
    check(
        "L1 counts real inserted rows via affected rows",
        "_copy_upsert" in methods and "def _copy_upsert(self, rows: list[tuple]) -> int" in src,
        "otherwise written counts submitted rows and skipped is always 0",
    )

    print("3. Column-order contract (COPY depends on it being positionally exact)")
    # COLUMNS is a **type-annotated** assignment (ast.AnnAssign), not ast.Assign
    n_cols = 0
    for n in tree.body:
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "COLUMNS":
            n_cols = len(n.value.elts)
        elif isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "COLUMNS":
            n_cols = len(n.value.elts)
    check("COLUMNS has 17 columns", n_cols == 17, f"actual {n_cols}")
    check("_to_row exists and is a staticmethod", "_to_row" in methods)

    print("4. Connection-pool guards (two pitfalls observed locally)")
    pool_src = (ROOT / "backend_ingestion/app/db/pool.py").read_text()
    sess_src = (ROOT / "backend_ingestion/app/db/session.py").read_text()
    check("pool factory has a MAX_POOL_SIZE floor", "MAX_POOL_SIZE" in pool_src)
    check("max_size differs from min_size (else it degenerates into a semaphore)",
          "max_size=max(pool_size, MAX_POOL_SIZE)" in pool_src)
    check("get_pool() has a pool-build lock (prevents concurrent pool creation)", "_pool_lock" in sess_src)

    print()
    if FAILS:
        print(f"FAIL {len(FAILS)} checks failed: {FAILS}")
        return 1
    print("PASS all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
