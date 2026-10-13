"""Schema 契约测试：`infra/init.sql` ←→ ORM(`db/models.py`) 必须逐列一致。

为什么需要这个测试
------------------
`AGENTS.md` §4「数据契约」要求「改动 Schema 必须同步更新 init.sql 与对应 ORM 模型」，
但此前仓库**没有任何机器可执行的校验** —— 全靠人工肉眼比对。
实际已经漏过一次：`robot_alert` 在 ORM 里被写成映射级复合主键 `(time, robot_id)`，
而 init.sql 根本没给这张表主键，两侧已经漂移很久无人发现。
订单 19 已把主键**真实落到两边**（`alert_id BIGSERIAL` + `PRIMARY KEY (time, alert_id)`），
本文件据此断言：DDL 含 PK、detail 为 JSONB、无 `__mapper_args__` 权宜之计、两侧列集合相等。

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


def _class_block(models_src: str, table: str) -> str | None:
    """返回 __tablename__ == table 的那个 class 块（到下一个 class 为止）。"""
    for block in re.split(r"\nclass ", "\n" + models_src):
        if f'__tablename__ = "{table}"' in block:
            return block
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


# ---------------------------------------------------------------- 订单 19 防漂移


def compile_orm_ddl(models_src: str, table: str) -> str:
    """把 models.py 里某张表的 ORM 定义编译成 PostgreSQL DDL 文本。

    直接用 `CreateTable(...).compile(postgresql.dialect())`，
    与订单 19 自检脚本里的命令同源，确保「测的就是验的」。

    实现要点：必须把模型模块**以真实模块名注册进 sys.modules** 再 exec ——
    SQLAlchemy 2.0 用 `Mapped[...]` 字符串注解时按 `__module__` 回查 globals，
    模块名不存在会 KeyError: 'Mapped'（实测）。注册后注解才解析得到。
    """
    import sys
    import types

    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    mod = types.ModuleType("_schema_contract_models")
    sys.modules[mod.__name__] = mod
    try:
        exec(compile(models_src, str(MODELS_PY), "exec"), mod.__dict__)
        return str(
            CreateTable(getattr(mod, table).__table__).compile(
                dialect=postgresql.dialect()
            )
        )
    finally:
        sys.modules.pop(mod.__name__, None)


def test_alert_orm_ddl_declares_primary_key() -> None:
    """① ORM 编译出的 DDL 必须含 `PRIMARY KEY (time, alert_id)`。

    订单 19 正题：旧实现靠映射级 `__mapper_args__` 造身份，不产生 DDL，
    ORM 与库内真实结构永远不一致。断言 PK 真实出现在 DDL 里，杜绝回潮。
    """
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert "PRIMARY KEY (time, alert_id)" in ddl, f"ORM DDL 缺主键：\n{ddl}"


def test_alert_orm_ddl_detail_is_jsonb() -> None:
    """② `detail` 编译结果必须是 JSONB（非 JSON）。

    旧分支多次把 JSONB 换成 sqlalchemy.JSON —— 编译成 JSON，
    丢索引/操作符能力且与 init.sql 漂移。
    """
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert re.search(r"detail\s+JSONB", ddl), f"detail 不是 JSONB：\n{ddl}"
    assert not re.search(r"detail\s+JSON\b(?!B)", ddl), f"detail 退化为 JSON：\n{ddl}"


def test_alert_no_mapper_args_workaround() -> None:
    """③ `RobotAlert` 不得再出现映射级 `__mapper_args__` 权宜之计。

    正主键落地后，任何 `__mapper_args__` 都是把身份从 DDL 里挪走的旧病复发。
    只查**代码**：先去掉类 docstring 与 `#` 注释，避免文档里提到该词造成误报。
    """
    block = _class_block(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert block is not None, "models.py 中找不到 RobotAlert"
    code = re.sub(r'"""((?!""").)*"""', "", block, flags=re.S)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    assert "__mapper_args__" not in code, "RobotAlert 仍带 __mapper_args__ 权宜之计"


def test_alert_columns_match_init_sql_exactly() -> None:
    """④ ORM 列集合 == init.sql:75-88 建表列集合（缺失/多余都报错）。

    这是本单的核心：消灭 ORM 与 DDL 的漂移。没有这条，下次改表还会再漂一次。
    """
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None, "robot_alert 未在 init.sql 中定义"
    orm = parse_orm_table(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert orm is not None, "robot_alert 未在 models.py 中建模"

    only_init = sorted(set(init["cols"]) - set(orm["cols"]))
    only_orm = sorted(set(orm["cols"]) - set(init["cols"]))
    assert not only_init, f"仅 init.sql 有的列：{only_init}"
    assert not only_orm, f"仅 ORM 有的列：{only_orm}"
    assert init["cols"] == orm["cols"], (
        f"列顺序不一致 init={init['cols']} orm={orm['cols']}"
    )
    # PK 逐字一致（顺序也一致）
    assert init["pk"] == orm["pk"] == ["time", "alert_id"], (
        f"主键不一致 init={init['pk']} orm={orm['pk']}"
    )


def test_alert_ack_fields_present() -> None:
    """⑤ ack_at / ack_by 两侧都在（本轮只建字段，不做 ack 接口）。"""
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None
    for col in ("ack_at", "ack_by"):
        assert col in init["cols"], f"init.sql robot_alert 缺列 {col}"


def test_alert_suppressed_fields_present_on_both_sides() -> None:
    """⑥ 订单 35：suppressed / suppressed_by 两侧都在，且 ORM 默认值为 FALSE。

    为什么这条必须存在（不是 `test_alert_columns_match_init_sql_exactly` 的重复）：
    后者只比**列集合**。suppressed 若在 ORM 里被写成 `default=True` 或 nullable，
    列集合照样相等，但库里的语义就反了 —— 每一条新告警都变成"已抑制"，
    告警流会整体静默。这里把默认值与可空性钉死。

    存量库迁移同样靠 `DEFAULT FALSE`：历史行语义必须保持不变。
    """
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None
    for col in ("suppressed", "suppressed_by"):
        assert col in init["cols"], f"init.sql robot_alert 缺列 {col}"

    # init.sql：suppressed 非空 + 显式 DEFAULT FALSE；suppressed_by 可空（未抑制时为 NULL）
    sql = INIT_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"suppressed\s+BOOLEAN\s+NOT NULL\s+DEFAULT FALSE", sql
    ), "init.sql 的 suppressed 必须是 BOOLEAN NOT NULL DEFAULT FALSE（历史行语义不变）"
    assert re.search(r"suppressed_by\s+TEXT\b", sql), "init.sql 的 suppressed_by 应为可空 TEXT"

    # ORM：同一语义。SQLAlchemy 编译出的 DDL 对 `server_default` 为空的列
    # **不会**输出 DEFAULT 子句（本机实测：`suppressed BOOLEAN NOT NULL`），
    # 默认值由 ORM 的 Python 侧 `default=False` 提供，而 init.sql 的 DEFAULT FALSE
    # 兜住直连 SQL 的写入。故这里只能钉「NOT NULL + 非默认 True」＋「源码里
    # default=False」，而不是期待 DDL 出现 DEFAULT 字样。
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert re.search(r"suppressed\s+BOOLEAN NOT NULL", ddl), (
        f"ORM suppressed 列属性漂移：\n{ddl}"
    )
    assert not re.search(r"suppressed\s+BOOLEAN[^,]*DEFAULT true", ddl, re.I), (
        f"ORM suppressed 默认值不得为 True（会让所有新告警都变'已抑制'）：\n{ddl}"
    )
    block = _class_block(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert block is not None
    assert re.search(
        r"suppressed:\s*Mapped\[bool\]\s*=\s*mapped_column\(\s*Boolean,\s*nullable=False,\s*default=False\s*\)",
        block,
    ), "ORM suppressed 必须显式 default=False（与 init.sql 的 DEFAULT FALSE 对齐）"
    assert re.search(
        r"suppressed_by:\s*Mapped\[str \| None\]\s*=\s*mapped_column\(\s*Text\s*\)",
        block,
    ), "ORM suppressed_by 应为可空 Text"


def main() -> int:
    checks = [
        ("init.sql 存在", test_init_sql_exists),
        ("models.py 存在", test_models_py_exists),
        ("Schema 契约（列 + 主键）", test_schema_contract),
        ("ORM DDL 含 PRIMARY KEY (time, alert_id)", test_alert_orm_ddl_declares_primary_key),
        ("ORM DDL detail 为 JSONB", test_alert_orm_ddl_detail_is_jsonb),
        ("无 __mapper_args__ 权宜之计", test_alert_no_mapper_args_workaround),
        ("两侧列集合逐列相等", test_alert_columns_match_init_sql_exactly),
        ("ack_at / ack_by 已建字段", test_alert_ack_fields_present),
        ("suppressed / suppressed_by 双侧一致（订单 35）", test_alert_suppressed_fields_present_on_both_sides),
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
