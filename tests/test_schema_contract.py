"""Schema contract test: `infra/init.sql` <-> ORM (`db/models.py`) must match column by column.

Why this test is needed
----------------------
`AGENTS.md` §4 "Data Contract" requires "changing the Schema must update init.sql and the
corresponding ORM model synchronously", but the repo previously had **no machine-executable
verification** -- it relied entirely on manual eyeballing. It already slipped once: `robot_alert`
was written in the ORM as a mapped composite primary key `(time, robot_id)`, while init.sql gave
this table no primary key at all, and the two sides drifted unnoticed for a long time. Order 19
finally landed the primary key on **both sides** (`alert_id BIGSERIAL` + `PRIMARY KEY (time, alert_id)`),
and this file asserts on that: the DDL contains a PK, detail is JSONB, there is no `__mapper_args__`
stopgap, and the two sides have equal column sets.

This test hardens the "manual comparison" into a repeatable assertion, no longer depending on attention.

Run
---
    python -m pytest tests/ -v      # with pytest
    python tests/test_schema_contract.py   # without pytest (this file has its own main)

Dependencies: standard library only (re / pathlib). Runs without pytest too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT_SQL = ROOT / "infra" / "init.sql"
MODELS_PY = ROOT / "backend_ingestion" / "app" / "db" / "models.py"

# Tables defined in init.sql (scope of this round: three business tables)
BUSINESS_TABLES = ("robot_telemetry", "robot_metadata", "robot_alert")


def parse_init_table(sql: str, table: str) -> dict | None:
    """Extract a table's column names and primary-key columns from init.sql.

    Supported forms:
    - Table-level `PRIMARY KEY (a, b)`
    - Inline column `robot_id TEXT PRIMARY KEY`
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
    """Extract a table's ORM columns and primary-key columns from models.py (split by class, tolerating in-class docstrings)."""
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
    """Return the class block whose __tablename__ == table (up to the next class)."""
    for block in re.split(r"\nclass ", "\n" + models_src):
        if f'__tablename__ = "{table}"' in block:
            return block
    return None


def collect_failures() -> list[str]:
    """Return all inconsistency descriptions; empty list = all passed."""
    sql = INIT_SQL.read_text(encoding="utf-8")
    src = MODELS_PY.read_text(encoding="utf-8")
    failures: list[str] = []

    for table in BUSINESS_TABLES:
        init = parse_init_table(sql, table)
        orm = parse_orm_table(src, table)

        if init is None:
            failures.append(f"{table}: table not found in init.sql (contract source missing)")
            continue
        if orm is None:
            failures.append(f"{table}: not modeled in models.py (ORM missing)")
            continue

        only_init = sorted(set(init["cols"]) - set(orm["cols"]))
        only_orm = sorted(set(orm["cols"]) - set(init["cols"]))
        if only_init:
            failures.append(f"{table}: columns only in init.sql {only_init} (ORM missing modeling)")
        if only_orm:
            failures.append(f"{table}: columns only in ORM {only_orm} (init.sql missing columns)")

        if sorted(init["pk"]) != sorted(orm["pk"]):
            failures.append(
                f"{table}: primary key mismatch init={sorted(init['pk'])} orm={sorted(orm['pk'])}"
            )

    return failures


def test_init_sql_exists() -> None:
    assert INIT_SQL.is_file(), f"Contract source missing: {INIT_SQL}"


def test_models_py_exists() -> None:
    assert MODELS_PY.is_file(), f"ORM source missing: {MODELS_PY}"


def test_schema_contract() -> None:
    failures = collect_failures()
    assert not failures, "Schema contract drift:\n  - " + "\n  - ".join(failures)


# ---------------------------------------------------------------- Order 19 anti-drift


def compile_orm_ddl(models_src: str, table: str) -> str:
    """Compile a table's ORM definition from models.py into PostgreSQL DDL text.

    Use `CreateTable(...).compile(postgresql.dialect())` directly, from the same source as the
    Order 19 self-check script, to ensure "what we test is what we verify".

    Implementation note: the model module must be registered in sys.modules **under its real
    module name** before exec -- SQLAlchemy 2.0 resolves `Mapped[...]` string annotations by
    looking up globals via `__module__`, and a missing module name raises KeyError: 'Mapped'
    (verified empirically). After registration the annotations resolve.
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
    """(1) The DDL compiled from the ORM must contain `PRIMARY KEY (time, alert_id)`.

    Order 19's core point: the old implementation faked identity via mapping-level
    `__mapper_args__`, producing no DDL, so the ORM and the real in-DB structure were
    forever inconsistent. Assert that the PK truly appears in the DDL to prevent regression.
    """
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert "PRIMARY KEY (time, alert_id)" in ddl, f"ORM DDL missing primary key:\n{ddl}"


def test_alert_orm_ddl_detail_is_jsonb() -> None:
    """(2) `detail`'s compiled result must be JSONB (not JSON).

    Old branches repeatedly swapped JSONB for sqlalchemy.JSON -- compiled to JSON, losing
    index/operator capabilities and drifting from init.sql.
    """
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert re.search(r"detail\s+JSONB", ddl), f"detail is not JSONB:\n{ddl}"
    assert not re.search(r"detail\s+JSON\b(?!B)", ddl), f"detail regressed to JSON:\n{ddl}"


def test_alert_no_mapper_args_workaround() -> None:
    """(3) `RobotAlert` must no longer use the mapping-level `__mapper_args__` stopgap.

    Once the real primary key landed, any `__mapper_args__` is a relapse of the old trick of
    moving identity out of the DDL. Only inspect **code**: first strip class docstrings and `#`
    comments to avoid false positives from the word merely appearing in documentation.
    """
    block = _class_block(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert block is not None, "RobotAlert not found in models.py"
    code = re.sub(r'"""((?!""").)*"""', "", block, flags=re.S)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    assert "__mapper_args__" not in code, "RobotAlert still carries the __mapper_args__ stopgap"


def test_alert_columns_match_init_sql_exactly() -> None:
    """(4) ORM column set == init.sql:75-88 create-table column set (missing/extra both error).

    This is the core of the ticket: eliminate ORM vs DDL drift. Without this, the next schema
    change would drift again.
    """
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None, "robot_alert not defined in init.sql"
    orm = parse_orm_table(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert orm is not None, "robot_alert not modeled in models.py"

    only_init = sorted(set(init["cols"]) - set(orm["cols"]))
    only_orm = sorted(set(orm["cols"]) - set(init["cols"]))
    assert not only_init, f"Columns only in init.sql: {only_init}"
    assert not only_orm, f"Columns only in ORM: {only_orm}"
    assert init["cols"] == orm["cols"], (
        f"Column order mismatch init={init['cols']} orm={orm['cols']}"
    )
    # PK matches verbatim (including order)
    assert init["pk"] == orm["pk"] == ["time", "alert_id"], (
        f"Primary key mismatch init={init['pk']} orm={orm['pk']}"
    )


def test_alert_ack_fields_present() -> None:
    """(5) ack_at / ack_by present on both sides (this round only creates fields, no ack API)."""
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None
    for col in ("ack_at", "ack_by"):
        assert col in init["cols"], f"init.sql robot_alert missing column {col}"


def test_alert_suppressed_fields_present_on_both_sides() -> None:
    """(6) Order 35: suppressed / suppressed_by present on both sides, and the ORM default is FALSE.

    Why this must exist (not a duplicate of `test_alert_columns_match_init_sql_exactly`):
    the latter only compares the **column set**. If suppressed is written as `default=True` or
    nullable in the ORM, the column sets are still equal, but the in-DB semantics are inverted --
    every new alert becomes "already suppressed" and the alert stream goes silently quiet. Here we
    pin the default value and nullability.

    Existing-database migration also relies on `DEFAULT FALSE`: historical rows' semantics must stay unchanged.
    """
    init = parse_init_table(INIT_SQL.read_text(encoding="utf-8"), "robot_alert")
    assert init is not None
    for col in ("suppressed", "suppressed_by"):
        assert col in init["cols"], f"init.sql robot_alert missing column {col}"

    # init.sql: suppressed is NOT NULL + explicit DEFAULT FALSE; suppressed_by is nullable (NULL when not suppressed)
    sql = INIT_SQL.read_text(encoding="utf-8")
    assert re.search(
        r"suppressed\s+BOOLEAN\s+NOT NULL\s+DEFAULT FALSE", sql
    ), "init.sql's suppressed must be BOOLEAN NOT NULL DEFAULT FALSE (historical rows' semantics unchanged)"
    assert re.search(r"suppressed_by\s+TEXT\b", sql), "init.sql's suppressed_by should be nullable TEXT"

    # ORM: same semantics. SQLAlchemy's compiled DDL for columns with empty `server_default`
    # does **not** emit a DEFAULT clause (verified locally: `suppressed BOOLEAN NOT NULL`); the
    # default is provided by the ORM's Python-side `default=False`, while init.sql's DEFAULT FALSE
    # covers direct SQL writes. So here we can only pin "NOT NULL + default not True" + "default=False
    # in source", rather than expect a DEFAULT clause in the DDL.
    ddl = compile_orm_ddl(MODELS_PY.read_text(encoding="utf-8"), "RobotAlert")
    assert re.search(r"suppressed\s+BOOLEAN NOT NULL", ddl), (
        f"ORM suppressed column property drifted:\n{ddl}"
    )
    assert not re.search(r"suppressed\s+BOOLEAN[^,]*DEFAULT true", ddl, re.I), (
        f"ORM suppressed default must not be True (would make all new alerts 'suppressed'):\n{ddl}"
    )
    block = _class_block(MODELS_PY.read_text(encoding="utf-8"), "robot_alert")
    assert block is not None
    assert re.search(
        r"suppressed:\s*Mapped\[bool\]\s*=\s*mapped_column\(\s*Boolean,\s*nullable=False,\s*default=False\s*\)",
        block,
    ), "ORM suppressed must explicitly set default=False (aligned with init.sql's DEFAULT FALSE)"
    assert re.search(
        r"suppressed_by:\s*Mapped\[str \| None\]\s*=\s*mapped_column\(\s*Text\s*\)",
        block,
    ), "ORM suppressed_by should be nullable Text"


def main() -> int:
    checks = [
        ("init.sql exists", test_init_sql_exists),
        ("models.py exists", test_models_py_exists),
        ("Schema contract (columns + primary key)", test_schema_contract),
        ("ORM DDL contains PRIMARY KEY (time, alert_id)", test_alert_orm_ddl_declares_primary_key),
        ("ORM DDL detail is JSONB", test_alert_orm_ddl_detail_is_jsonb),
        ("no __mapper_args__ stopgap", test_alert_no_mapper_args_workaround),
        ("both column sets equal column-by-column", test_alert_columns_match_init_sql_exactly),
        ("ack_at / ack_by fields created", test_alert_ack_fields_present),
        ("suppressed / suppressed_by consistent on both sides (Order 35)", test_alert_suppressed_fields_present_on_both_sides),
    ]
    failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
