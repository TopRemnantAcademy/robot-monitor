"""DB 对接的端到端验证（真实 PostgreSQL，非 mock）。

跑法（需要可写的 PostgreSQL + 已执行 infra/init.sql 的纯 PG 部分）：

    export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/robot_monitor
    cd backend_ingestion && python -m tests.verify_db_integration

⚠️ 本文件**不是** pytest 用例，原名 `test_db_integration.py` 已被改掉：
断言全部写在 `t1..t6` 里、由 `main()` 驱动，pytest 收集结果为 **0 tests** ——
名字带 `test_` 会造成「有测试在跑」的假象（与 alert_engine 的 `test_notifier.py`
同一类假覆盖缺陷）。它是**按需运行的独立脚本**，退出码 0 = 全部通过。

覆盖（每条都对应本轮修掉的一个真实缺陷，不是凑数的断言）：
  T1 COPY 路径写入真实 JSONB 表，且 jsonb 列类型未被破坏
  T2 COPY 路径幂等（重复批次不涨行、不报错）—— 这是 COPY 无 ON CONFLICT 的补丁
  T3 降级链：强制 L1 失败 → L2 生效
  T4 坏行隔离：一行脏数据不连坐整批
  T5 写入吞吐相对 1000 行/s 峰值的冗余倍数
  T6 ORM 模型 DDL 与 infra/init.sql 的列类型一致（JSONB 漂移回归）

需要 timescaledb 扩展的语句（create_hypertable / 连续聚合 / 保留策略）不在本文件覆盖范围，
它们已标注「未验证」，验证条件写在该文件注释里。
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
    # 必须在 import app.config 之前注入：settings 是模块级单例，
    # 后面再改 database_url 不会让已构造的 Settings 生效。
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
            # 关键：客户端毫秒时间戳必须不同，否则主键冲突会被静默丢行
            "ts": 1767225600000 + i,
        }
    )


async def t1_copy_jsonb(conn: asyncpg.Connection) -> None:
    print("\nT1 COPY 路径写入真实 JSONB 表")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(200)]
    await w._copy_upsert(rows)
    n = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("落库行数正确", n == 200, f"{n}/200")
    types = await conn.fetchrow(
        """SELECT jsonb_typeof(joint_temperatures) jt, jsonb_typeof(joint_positions) jp,
                  jsonb_typeof(joint_torques) jto FROM robot_telemetry LIMIT 1"""
    )
    check("三个关节列仍是 jsonb（未被降级成 text/json）",
          tuple(types) == ("object", "object", "object"), str(dict(types)))
    v = await conn.fetchval(
        "SELECT joint_temperatures->>'left_shoulder_pitch' FROM robot_telemetry LIMIT 1")
    check("jsonb 可用 ->> 取值（连续聚合/热力图前提）", v == "42.5", str(v))


async def t2_idempotent(conn: asyncpg.Connection) -> None:
    print("\nT2 COPY 路径幂等（COPY 本身不支持 ON CONFLICT）")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(200)]
    await w._copy_upsert(rows)
    first = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    # 重试场景：同一批再写一遍，必须不报错、不涨行
    err = None
    try:
        await w._copy_upsert(rows)
    except Exception as exc:            # noqa: BLE001 — 这里就是要抓任意异常
        err = exc
    second = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("重复批次不抛异常", err is None, repr(err))
    check("重复批次行数不变（幂等）", first == second == 200, f"{first} -> {second}")


async def t3_fallback(conn: asyncpg.Connection) -> None:
    print("\nT3 降级链：L1 失败 → L2 接管")
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
    check("L1 全败后 L2 把数据写进去", n == 200, f"{n}/200")
    check("重试被计数（可观测）", w.retries > 0, f"retries={w.retries}")


async def t4_isolation(conn: asyncpg.Connection) -> None:
    print("\nT4 坏行隔离：一行脏数据不连坐整批")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    rows = [w._to_row(make_telemetry(i)) for i in range(100)]
    # 在中间塞一行不可写的（max_joint_temp 是 NOT NULL，置 None → NotNullViolation）
    bad = list(rows[0])
    bad[1] = "humanoid-099"
    bad[6] = None                       # max_joint_temp = NULL
    bad[0] = datetime.fromtimestamp(1767225600.0 + 999, tz=timezone.utc)
    rows[50] = tuple(bad)

    before = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    await w._isolate_and_write(rows)
    after = await conn.fetchval("SELECT count(*) FROM robot_telemetry")
    check("好数据被保住（99 条）", after == 99, f"{after}/99（坏行 1 条）")
    check("坏行计入 rejected", w.rejected == 1, f"rejected={w.rejected}")
    check("坏行确实没落库", after == before + 99)


async def t5_throughput(conn: asyncpg.Connection) -> None:
    print("\nT5 写入吞吐相对峰值的冗余")
    await conn.execute("TRUNCATE robot_telemetry")
    w = BatchWriter()
    n = 5000
    rows = [w._to_row(make_telemetry(i)) for i in range(n)]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await w._copy_upsert(rows)
    dt = loop.time() - t0
    rps = n / dt
    check("COPY 吞吐 > 1000 行/s 峰值 × 10", rps > 10000,
          f"{rps:,.0f} 行/s（{rps/1000:.0f}x 冗余）")


async def t6_schema_parity(conn: asyncpg.Connection) -> None:
    print("\nT6 ORM DDL 与 infra/init.sql 列类型一致")
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
            mismatched.append(f"{col.name}: 库中不存在")
            continue
        # JSONB 在 information_schema 里 data_type='jsonb'
        expected = col.type.__class__.__name__.lower()
        if expected == "jsonb" and actual != "jsonb":
            mismatched.append(f"{col.name}: ORM=jsonb 库={actual}")
        if expected == "json" and actual != "json":
            mismatched.append(f"{col.name}: ORM=json but 库={actual}")
    check("关节列 ORM 类型 == 库内 jsonb（无漂移）", not mismatched, "; ".join(mismatched))
    check("COLUMNS 数量 == 表列数", len(COLUMNS) == len(db_types),
          f"{len(COLUMNS)} vs {len(db_types)}")


async def main() -> int:
    if not DSN:
        print("跳过：未设置 TEST_DATABASE_URL（本测试需要真实 PostgreSQL）")
        return 0
    conn = await asyncpg.connect(DSN)
    try:
        print(f"== DB 对接端到端验证 ==\n{DSN}")
        for fn in (t1_copy_jsonb, t2_idempotent, t3_fallback, t4_isolation, t5_throughput, t6_schema_parity):
            await fn(conn)
    finally:
        await conn.close()
    print(f"\n== 结果：{PASS} 通过 / {FAIL} 失败 ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
