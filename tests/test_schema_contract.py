"""Schema 契约测试：`infra/init.sql` ←→ ORM(`db/models.py`) 必须逐列一致。

为什么需要这个测试
------------------
`AGENTS.md` §4「数据契约」要求「改动 Schema 必须同步更新 init.sql 与对应 ORM 模型」，
但此前仓库**没有任何机器可执行的校验** —— 全靠人工肉眼比对。
实际已经漏过一次：`robot_alert` 在 ORM 里被写成复合主键 `(time, robot_id)`，
而 init.sql 根本没给这张表主键，两侧已经漂移很久无人发现。

本测试把「人工比对」固化为可重复运行的断言，不再依赖注意力。

运行
----
    python -m pytest tests/ -v      # 有 pytest 时
    python tests/test_schema_contract.py   # 无 pytest 时（本文件自带 main）

依赖：仅标准库（re / pathlib）。不引入 pytest 也能跑。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_SQL = ROOT / "infra" / "init.sql"
MODELS_PY = ROOT / "backend_ingestion" / "app" / "db" / "models.py"

# init.sql 里定义的表（本轮范围：三张业务表）
BUSINESS_TABLES = ("robot_telemetry", "robot_metadata", "robot_alert")


def parse_init_table(sql: str, table: str) -> dict | None:
    """从 init.sql 抽取某张表的列名与主键列。

    支持的写法：
    - 表级 `PRIMARY KEY (a, b)`
    - 列内联 `robot_id TEXT PRIMARY KEY`
    """
    m = re.search(r"CREATE TABLE IF NOT EXISTS " + table + r"\s*\((.*?)\n\);", sql, re.S)
    if not m:
        return None

    cols: list[str] = []
    pk: list[str] = []
    for raw in m.group(1).splitlines():
        line = raw.split("--")[0].strip().rstrip(",")
        if not line:
            continue

        pm = re.match(r"PRIMARY KEY\s*\(([^)]*)\)", line, re.I)
        if pm:
            pk = [x.strip() for x in pm.group(1).split(",")]
            continue
        if re.match(r"^(CONSTRAINT|UNIQUE|CHECK|FOREIGN)", line, re.I):
            continue

        cm = re.match(r"^(\w+)\s+\w", line)
        if not cm:
            continue
        col = cm.group(1)
        cols.append(col)
        if re.search(r"\bPRIMARY KEY\b", line, re.I) and col not in pk:
            pk.append(col)

    return {"cols": cols, "pk": pk}


def parse_orm_table(models_src: str, table: str) -> dict | None:
    """从 models.py 抽取某张表对应的 ORM 列与主键列（按类切块，兼容类内 docstring）。"""
    for block in re.split(r"\nclass ", "\n" + models_src):
        if f'__tablename__ = "{table}"' not in block:
            continue
        cols: list[str] = []
        pk: list[str] = []
        for line in block.splitlines():
            cm = re.match(r"\s+(\w+):\s*Mapped\[", line)
            if cm:
                cols.append(cm.group(1))
                if "primary_key=True" in line:
                    pk.append(cm.group(1))
        return {"cols": cols, "pk": pk}
    return None


def collect_failures() -> list[str]:
    """返回所有不一致描述；空列表 = 全部通过。"""
    sql = INIT_SQL.read_text(encoding="utf-8")
    src = MODELS_PY.read_text(encoding="utf-8")
    failures: list[str] = []

    for table in BUSINESS_TABLES:
        init = parse_init_table(sql, table)
        orm = parse_orm_table(src, table)

        if init is None:
            failures.append(f"{table}: init.sql 中找不到该表（契约源缺失）")
            continue
        if orm is None:
            failures.append(f"{table}: models.py 中未建模（ORM 缺失）")
            continue

        only_init = sorted(set(init["cols"]) - set(orm["cols"]))
        only_orm = sorted(set(orm["cols"]) - set(init["cols"]))
        if only_init:
            failures.append(f"{table}: 仅 init.sql 有的列 {only_init}（ORM 漏建模）")
        if only_orm:
            failures.append(f"{table}: 仅 ORM 有的列 {only_orm}（init.sql 漏加列）")

        if sorted(init["pk"]) != sorted(orm["pk"]):
            failures.append(
                f"{table}: 主键不一致 init={sorted(init['pk'])} orm={sorted(orm['pk'])}"
            )

    return failures


def test_init_sql_exists() -> None:
    assert INIT_SQL.is_file(), f"契约源缺失：{INIT_SQL}"


def test_models_py_exists() -> None:
    assert MODELS_PY.is_file(), f"ORM 源缺失：{MODELS_PY}"


def test_schema_contract() -> None:
    failures = collect_failures()
    assert not failures, "Schema 契约漂移：\n  - " + "\n  - ".join(failures)


def test_alert_table_has_no_primary_key() -> None:
    """回归护栏：robot_alert 是 hypertable 且原表无 PK。

    历史缺陷：ORM 曾把它写成 (time, robot_id) 复合主键，
    会导致同机器同毫秒只能存一条告警（与去重窗口语义冲突）。
    """
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None, "robot_alert 未在 init.sql 中定义"
    assert init["pk"] == [], f"robot_alert 不应有主键，实际 {init['pk']}"


def main() -> int:
    checks = [
        ("init.sql 存在", test_init_sql_exists),
        ("models.py 存在", test_models_py_exists),
        ("Schema 契约（列 + 主键）", test_schema_contract),
        ("robot_alert 无主键（回归护栏）", test_alert_table_has_no_primary_key),
    ]
    failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
