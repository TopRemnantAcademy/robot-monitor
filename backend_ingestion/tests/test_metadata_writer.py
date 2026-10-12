"""ISSUE #96 — robot_metadata 心跳的**批量语义**验收（纯内存，零外部依赖）。

覆盖两条禁止项与两条语义要求，全部断言落在 `MetadataWriter` 的真实行为上：

1. **禁止逐条 UPDATE** —— 1000 条遥测（100 台 × 10 次）只能折算成 1 条 UPDATE 语句，
   且参数数组里恰好 100 个 robot_id（按主键折叠）。
2. **禁止改 init.sql 列定义** —— SQL 文本只用 `is_online` / `last_seen` 两列，
   没有发明第三列，也没有 DDL。
3. **周期置 false** 用的是 `state_ttl_seconds`，不是一个另起的魔数。
4. **开关关闭时不写**：`metadata_heartbeat_enabled=False` 下 flush() 一条语句都不发，
   保证「不得改变既有遥测写路径」这条约束可验证。

跑法：`pytest backend_ingestion/tests/`（settings.jwt_secret 由 conftest 注入）。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from app.services import metadata_writer as mw
from app.services.metadata_writer import MARK_OFFLINE_SQL, UPDATE_SQL, MetadataWriter


class _FakeConn:
    """记录所有下发语句的假连接（不连库）。"""

    def __init__(self, offline_rows=None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._offline_rows = offline_rows or []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        # asyncpg 的 UPDATE 状态串形如 "UPDATE 7"
        return f"UPDATE {len(args[0]) if args else 0}"

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return [{"robot_id": r} for r in self._offline_rows]


class _FakePool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _writer_with(monkeypatch, offline_rows=None) -> tuple[MetadataWriter, _FakeConn]:
    conn = _FakeConn(offline_rows)

    async def fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(mw, "get_pool", fake_get_pool)
    return MetadataWriter(), conn


def test_1000_telemetry_folds_into_one_update(monkeypatch):
    """1000 条遥测（100 台 × 10Hz × 1s）→ **1 条** UPDATE，且只有 100 个 robot_id。"""
    w, conn = _writer_with(monkeypatch)
    for _ in range(10):
        for i in range(1, 101):
            w.put(f"humanoid-{i:03d}")

    asyncio.run(w.flush())

    updates = [(sql, args) for sql, args in conn.calls if "unnest" in sql]
    assert len(updates) == 1, f"应折叠成 1 条 UPDATE，实际 {len(updates)}"
    ids, seen = updates[0][1]
    assert len(ids) == 100, f"robot_id 应折叠到 100 个，实际 {len(ids)}"
    assert len(seen) == 100
    assert all(isinstance(v, datetime) and v.tzinfo is not None for v in seen)
    assert w.stats["deduped"] == 900, "被折叠掉的重复条数应为 900"
    assert w.stats["rows_written"] == 100


def test_telemetry_count_does_not_change_statement_count(monkeypatch):
    """频次不放大写放大：10 条 vs 10000 条遥测，UPDATE 语句数都是 1。"""
    counts = []
    for n in (10, 10000):
        w, conn = _writer_with(monkeypatch)
        for i in range(n):
            w.put(f"humanoid-{(i % 100) + 1:03d}")
        asyncio.run(w.flush())
        counts.append(sum(1 for sql, _ in conn.calls if "unnest" in sql))
    assert counts == [1, 1], f"语句数随条数增长：{counts}"


def test_offline_sweep_uses_state_ttl_seconds(monkeypatch):
    """周期置 false 的阈值必须来自 settings.state_ttl_seconds（默认 10s）。"""
    from app.config import settings

    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-007"])
    asyncio.run(w.flush())

    sweeps = [(sql, args) for sql, args in conn.calls if "is_online = FALSE" in sql]
    assert len(sweeps) == 1, "每次刷新应有且仅有一次离线扫描"
    assert sweeps[0][1][0] == float(settings.state_ttl_seconds)
    assert w.stats["offline_marks"] == 1


def test_next_flush_still_sweeps_for_offline(monkeypatch):
    """空窗口也要扫描：机器人停发遥测后队列是空的，漏掉扫描就永远不会置 false。"""
    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-001", "humanoid-002"])
    asyncio.run(w.flush())                    # 队列空
    assert sum(1 for sql, _ in conn.calls if "is_online = FALSE" in sql) == 1
    assert w.stats["rows_written"] == 0


def test_sql_only_writes_existing_columns():
    """禁止改 init.sql 列定义 → 写入的列只允许是 is_online / last_seen。"""
    # 心跳语句的 SET 子句
    sets = re.findall(r"SET\s+(.+?)\s+FROM\s", UPDATE_SQL, flags=re.IGNORECASE | re.DOTALL)
    assert len(sets) == 1, f"心跳 UPDATE 应恰好一个 SET 子句: {UPDATE_SQL}"
    written = {c.split("=")[0].strip() for c in sets[0].split(",")}
    assert written == {"last_seen", "is_online"}, f"写入了新列: {written}"

    sets = re.findall(r"SET\s+(.+?)\s+WHERE\s", MARK_OFFLINE_SQL, flags=re.IGNORECASE | re.DOTALL)
    assert [s.strip() for s in sets] == ["is_online = FALSE"]

    for sql in (UPDATE_SQL, MARK_OFFLINE_SQL):
        assert "robot_metadata" in sql
        assert "ALTER TABLE" not in sql.upper(), "禁止 DDL 改动"


def test_heartbeat_uses_server_clock_when_ts_missing(monkeypatch):
    """payload 无 ts 时必须退回本机时钟，而不是写入 1970 或 None。"""
    w, conn = _writer_with(monkeypatch)
    w.put("humanoid-001", None)
    asyncio.run(w.flush())
    _, seen = next((sql, a) for sql, a in conn.calls if "unnest" in sql)[1]
    assert seen[0].year >= 2024, f"时间戳异常: {seen[0]!r}"


def test_heartbeat_ignores_stale_payload_ts(monkeypatch):
    """**回归**：payload 的 ts 落后服务器时，`last_seen` 仍必须是服务端当前时刻。

    实测故障（本单开发中踩到）：`put(rid, ts_ms)` 若把 payload 时间戳当
    `last_seen` 写库，一个时钟落后 / 重放历史 / 离线缓发的客户端会让
    `MARK_OFFLINE_SQL` 的 `last_seen < NOW() - ttl` 谓词在同一轮刷新里
    把它刚写成 TRUE 的行立刻翻回 FALSE —— 「5 台刚上报、紧接一句全被判离线」。

    这里用 2023-11 的时间戳（远早于 now-10s）复现：断言落库的时间戳是**现在**。
    若实现回退成 `ts_ms / 1000.0`，`seen[0].year` 会变成 2023 而变红。
    """
    import time as _time
    from datetime import datetime, timezone

    w, conn = _writer_with(monkeypatch)
    stale_ms = 1700000000000                      # 2023-11-14，必然 > ttl
    w.put("humanoid-001", stale_ms)
    asyncio.run(w.flush())

    _, seen = next((sql, a) for sql, a in conn.calls if "unnest" in sql)[1]
    age = _time.time() - seen[0].timestamp()
    assert seen[0].year >= 2024, f"写入了 payload 的旧时间戳: {seen[0]!r}"
    assert abs(age) < 5, f"last_seen 偏离服务端现在 {age:.1f}s（应≈0）"
    assert seen[0] != datetime.fromtimestamp(stale_ms / 1000.0, tz=timezone.utc)


def test_stop_flushes_pending_before_return(monkeypatch):
    """关闭时不能丢在飞的心跳：stop() 后 run() 退出，但存量必须已落库。"""
    w, conn = _writer_with(monkeypatch)
    w.put("humanoid-001")

    async def scenario():
        task = asyncio.create_task(w.run())
        await w.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert w.stats["rows_written"] == 1, "stop() 前入队的心跳被丢了"
    assert w.stats["flushes"] >= 1


def test_disabled_switch_writes_nothing(monkeypatch):
    """开关关闭 → flush() 不发任何语句（保证不改既有遥测写路径的语义与吞吐）。"""
    from app.config import settings

    w, conn = _writer_with(monkeypatch, offline_rows=["humanoid-001"])
    monkeypatch.setattr(settings, "metadata_heartbeat_enabled", False)

    w.put("humanoid-001")
    asyncio.run(w.flush())

    assert conn.calls == [], f"开关关闭时仍有写语句下发：{conn.calls}"
    assert w.stats["rows_written"] == 0
    assert w.stats["offline_marks"] == 0


def test_disabled_switch_makes_run_return_immediately(monkeypatch):
    """开关关闭 → run() 立即退出（不必等 close_pool 前的 stop()）。"""
    from app.config import settings

    w, _ = _writer_with(monkeypatch)
    monkeypatch.setattr(settings, "metadata_heartbeat_enabled", False)

    async def scenario():
        await asyncio.wait_for(w.run(), timeout=1)

    asyncio.run(scenario())      # 不超时即通过
