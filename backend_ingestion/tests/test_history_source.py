"""`/history` 数据源切换与回退（ISSUE #76）。

为什么单独一个文件、且**不**依赖真实 TimescaleDB：
本单的验收点里有一条最容易被写成"人眼比对通过" —— 「改了连续聚合但字段漏了」。
订单 16 已真实翻车过一次（漏 `joint_positions` → Drawer 第三条折线恒空），
STATUS.md §六第 2 条留了记录。所以这里把三件事做成**机器可判定**的断言：

1. 聚合分支与回退分支的 **SELECT 列集合逐字相等** —— 否则一侧加列另一侧漏列，
   就会出现"平时正常、一刷新窗口就又变空"的间歇故障，比全坏更难查；
2. `joint_positions / joint_temperatures / joint_torques` 两分支**都**在列里
   （ISSUE #76 的约束项：不得移除 `last(joint_positions, time)`）；
3. 走聚合时不查原始表、聚合返回空时才回退 —— 用可数的 fake 连接锁定调用序列，
   `source` 计数标签同步断言（p95 下降必须能归因到"真的换了数据源"）。

用 fake pool 而非真实 PG：本环境无 TimescaleDB，真实库测不了；
而这里要考的是**控制流与 SQL 契约**，两者都在纯 Python 层可判定。
真实 SQL 走通由 R-09 §4 的实测记录与 PR「未验证」章节分别说明。

跑法：
    cd backend_ingestion && python -m pytest tests/test_history_source.py -v
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re

import pytest

os.environ.setdefault("JWT_SECRET", "test-only-secret-0123456789abcdef")

from app import main as main_mod  # noqa: E402

# 两分支都必须出现的三个 JSONB 关节列（ISSUE #76 验收 / 订单 16 事故回归）。
JOINT_COLS = ("joint_temperatures", "joint_positions", "joint_torques")


def _select_body(sql: str) -> str:
    """取 SELECT 与 FROM 之间的列清单（归一化空白），用于逐字比对两分支。"""
    m = re.search(r"SELECT(.*?)\n\s*FROM", sql, re.S)
    assert m, f"SQL 里找不到 SELECT ... FROM：{sql!r}"
    # 去掉 SQL 注释（`-- ...` 到行尾），它们只在原始表分支出现，参与比对会假红。
    body = re.sub(r"--[^\n]*", "", m.group(1))
    return re.sub(r"\s+", " ", body).strip()


class _FakeConn:
    """按预设脚本逐次返回结果的假连接，同时记录每次实际执行的 SQL。"""

    def __init__(self, script: list[list[dict]]):
        self._script = list(script)
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        assert self._script, "fetch 被调用的次数超出预设脚本"
        return self._script.pop(0)


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _row(bucket: _dt.datetime) -> dict:
    return {
        "bucket": bucket,
        "robot_id": "humanoid-001",
        "status_code": 1,
        "battery_pct": 88.5,
        "max_joint_temp": 51.2,
        "joint_temperatures": {"left_knee_pitch": 49.1},
        "joint_positions": {"left_knee_pitch": 0.5},
        "joint_torques": {"left_knee_pitch": 3.4},
    }


def _run_history(monkeypatch, conn, minutes=5, resolution="10s"):
    """以假池跑一次 `/history`，返回 (响应 dict, 假连接)。

    `minutes` / `resolution` 可覆写（R-12 需要按跨度触发小时路径），
    默认值保持原样，故既有调用点语义不变。
    """
    from app.db import session as session_mod

    async def _fake_get_pool():
        return _FakePool(conn)

    monkeypatch.setattr(session_mod, "get_pool", _fake_get_pool)
    return asyncio.run(
        main_mod.history(
            "humanoid-001", minutes=minutes, resolution=resolution, _user=None
        )
    )


@pytest.fixture(autouse=True)
def _no_metrics_noise(monkeypatch):
    """本文件只考控制流，不重复观察 Histogram 的真实耗时。"""
    monkeypatch.setattr(main_mod.metrics.history_latency_seconds, "observe", lambda *a: None)


class TestSqlContract:
    """纯 SQL 文本断言 —— 不需要任何 fake 就能跑，最快的护栏。"""

    def test_all_joint_columns_in_both_branches(self):
        """三个关节列必须同时出现在**两个**分支 —— 这是本单的核心不变量。"""
        for name, sql in _live_sqls().items():
            body = _select_body(sql)
            for col in JOINT_COLS:
                assert f"last({col}, time) AS {col}" in body, (
                    f"{name} 分支缺 `last({col}, time) AS {col}`；"
                    f"实际列清单：{body}"
                )

    def test_branch_columns_identical_apart_from_bucket_expr(self):
        """两分支除 bucket **表达式**外，列清单必须逐字相同。

        为什么把 bucket 排除在外：两分支的 bucket 来源本就不同 —— 聚合分支直接读
        物化列 `bucket`，原始表分支必须现算 `time_bucket(...)`。这是**设计如此**，
        不是漂移。真正要锁死的是「其余每一列、每个聚合函数、每个别名都一致」，
        否则一侧加列另一侧漏列，就会出现"平时正常、一刷新窗口就又变空"的间歇故障。
        同时单独断言 bucket 列在两分支里都**别名成 bucket**（响应字段名靠它）。
        """
        sqls = _live_sqls()
        stripped = {}
        for name, sql in sqls.items():
            body = _select_body(sql)
            # 把 bucket 那一列的**表达式**换成中性字面量，只保留别名。
            # 非贪婪匹配到 `AS bucket` 为止，避开 time_bucket(...) 里的嵌套括号。
            body = re.sub(
                r"^.*?\sAS bucket",
                "BUCKET_EXPR AS bucket",
                body,
                count=1,
            )
            stripped[name] = body
        assert stripped["cagg"] == stripped["raw"], (
            "聚合分支与回退分支的列已漂移（bucket 表达式已中性化）：\n"
            f"  cagg: {stripped['cagg']}\n  raw : {stripped['raw']}"
        )
        for name, body in stripped.items():
            assert body.startswith("BUCKET_EXPR AS bucket"), f"{name} 分支 bucket 未别名成 bucket"

    def test_cagg_branch_reads_the_continuous_aggregate(self):
        assert "FROM robot_history_10s" in _live_sqls()["cagg"]

    def test_cagg_branch_does_not_time_bucket_again(self):
        """聚合已是 10s 桶，再套一层 time_bucket 属重复聚合（且会掩盖索引）。"""
        assert "time_bucket" not in _live_sqls()["cagg"]

    def test_cagg_branch_filters_on_bucket_column(self):
        """过滤必须落在物化列 `bucket` 上，才能吃到连续聚合自动建的 (robot_id, bucket) 索引。"""
        sql = _live_sqls()["cagg"]
        assert "bucket >= now()" in sql

    def test_raw_branch_still_time_buckets(self):
        assert "time_bucket" in _live_sqls()["raw"]
        assert "FROM robot_telemetry" in _live_sqls()["raw"]


def _live_sqls() -> dict:
    """取 /history 运行期真正使用的两条 SQL。

    为避免「测试自己复刻 SQL 字符串、和产品代码各写一份」这种自欺，
    这里直接跑一次 /history，从假连接记录里取出真实 SQL。
    """
    return _captured_sqls


# 模块级缓存：import 后跑一次空结果 /history，把两条真实 SQL 抓出来。
class _CaptureConn:
    def __init__(self):
        self.calls: list[str] = []

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        return []  # 空结果 → 触发回退 → 两条 SQL 都会被调用


_cap = _CaptureConn()
_captured_sqls: dict = {}


def _prime_sqls():
    from app.db import session as session_mod

    orig = session_mod.get_pool

    async def _fake():
        return _FakePool(_cap)

    session_mod.get_pool = _fake
    try:
        asyncio.run(main_mod.history("humanoid-001", minutes=5, resolution="10s", _user=None))
    finally:
        session_mod.get_pool = orig
    assert len(_cap.calls) == 2, f"预期聚合+回退各一次，实际 {len(_cap.calls)} 次"
    _captured_sqls["cagg"] = _cap.calls[0]
    _captured_sqls["raw"] = _cap.calls[1]


_prime_sqls()


class TestRouting:
    """控制流：何时命中聚合、何时回退，以及 source 计数能否归因。"""

    def test_cagg_hit_skips_raw_table(self, monkeypatch):
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        before = _counter("cagg")
        resp = _run_history(monkeypatch, conn)

        assert len(conn.calls) == 1, "命中聚合时不该再查原始表"
        assert "robot_history_10s" in conn.calls[0]
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}
        assert _counter("cagg") == before + 1

    def test_empty_cagg_falls_back_to_raw(self, monkeypatch):
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[], [_row(now)]])  # 聚合空 → 回退有数据
        before_raw = _counter("raw")
        resp = _run_history(monkeypatch, conn)

        assert len(conn.calls) == 2, "聚合为空时必须回退原始表"
        assert "robot_telemetry" in conn.calls[1]
        assert _counter("raw") == before_raw + 1, "回退必须计数，否则无法归因 p95"
        # 回退路径的 joint_positions 不得丢（ISSUE #76 / 订单 16 事故）
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}

    def test_both_empty_returns_empty_points_not_error(self, monkeypatch):
        """「从未上报过数据的机器人」是正常语义（main.py 原注释），不得变成 5xx。"""
        conn = _FakeConn([[], []])
        resp = _run_history(monkeypatch, conn)
        assert resp["points"] == []

    def test_query_failure_is_503_and_not_counted_as_cagg(self, monkeypatch):
        """聚合查询抛错 → 503，且**不得**被计入 source="cagg"。

        为什么单独测：`source` 若在异常路径也被记成 cagg，
        最该暴露的故障（聚合查不动）会被伪装成最理想的状态（聚合命中），
        面板上反而看好。这里用计数前后差值把这个反例钉死。
        """

        class _BoomConn:
            async def fetch(self, sql, *args):
                raise RuntimeError("boom")

        before_cagg = _counter("cagg")
        resp = _run_history(monkeypatch, _BoomConn())
        assert resp.status_code == 503
        assert _counter("cagg") == before_cagg, "失败路径不得计入 cagg"

    def test_invalid_resolution_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=5, resolution="500ms", _user=None)
            )
        assert ei.value.status_code == 400


def _counter(source: str) -> float:
    return main_mod.metrics.history_source.labels(source=source)._value.get()


# ───────────────────────── R-12：小时级下采样 ─────────────────────────
#
# R-12 的核心断言：`minutes` 超过阈值后**换数据源**（不是换参数）。
# 与上面 TestSqlContract 同构，这里考的是「小时路径的 SQL 指向 robot_history_1h、
# 列集与 10s 路径一致、响应 resolution 回显 3600」，以及上限 400。
#
# 为什么用 fake pool 而非真实库：本环境无 TimescaleDB（同文件头注释），
# 而「按跨度路由到哪张表」是纯控制流，可在 Python 层逐字判定。
# 真实 SQL 的语义正确性由 e2e 30k 跑通 + PR「未验证」章节说明。
def _capture_hourly_sql() -> dict:
    """跑一次大跨度 /history，抓出小时路径实际用的两条 SQL（聚合 + 回退）。"""
    conn = _FakeConn([[], []])  # 都空 → 聚合与回退各调一次
    from app.db import session as session_mod

    orig = session_mod.get_pool

    async def _fake():
        return _FakePool(conn)

    session_mod.get_pool = _fake
    try:
        asyncio.run(
            main_mod.history("humanoid-001", minutes=1440, resolution="10s", _user=None)
        )
    finally:
        session_mod.get_pool = orig
    assert len(conn.calls) == 2, f"预期聚合+回退各一次，实际 {len(conn.calls)} 次"
    return {"cagg": conn.calls[0], "raw": conn.calls[1]}


class TestHourlyRouting:
    """R-12 T2：跨度路由 + 列完整 + resolution 回显 + 上限。"""

    def test_large_span_routes_to_hourly_aggregate(self, monkeypatch):
        """minutes=1440（24h）→ 读 robot_history_1h，不再读 10s 视图（V3）。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440)

        assert len(conn.calls) == 1, "命中小时聚合时不该再查原始表"
        assert "robot_history_1h" in conn.calls[0]
        assert "robot_history_10s" not in conn.calls[0], "大跨度不该再读 10s 视图"

    def test_small_span_still_routes_to_10s(self, monkeypatch):
        """阈值内（minutes=180）仍走 10s —— R-03 §4 契约硬红线，不得被小时路径挤掉。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        _run_history(monkeypatch, conn, minutes=180)

        assert "robot_history_10s" in conn.calls[0]
        assert "robot_history_1h" not in conn.calls[0]

    def test_hourly_branch_keeps_joint_positions(self, monkeypatch):
        """V4：小时路径响应含 joint_positions（R-12 §6 硬红线，Drawer 第三折线）。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440)

        p = resp["points"][0]
        assert p["joint_positions"] == {"left_knee_pitch": 0.5}
        assert p["joint_temperatures"] == {"left_knee_pitch": 49.1}
        assert p["joint_torques"] == {"left_knee_pitch": 3.4}

    def test_hourly_resolution_echoed_as_3600s(self, monkeypatch):
        """小时路径 resolution 回显 3600s（真实桶秒数），不是入参 10s。

        回显入参会让前端把 1h 桶当 10s 桶画 —— 横轴间隔差 360 倍。
        """
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=1440, resolution="30s")
        assert resp["resolution"] == "3600s"

    def test_small_span_resolution_still_echoes_input(self, monkeypatch):
        """小路径 resolution 回显入参（逐字不变，R-03 回归）。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=5, resolution="30s")
        assert resp["resolution"] == "30s"

    def test_hourly_empty_falls_back_to_raw(self, monkeypatch):
        """T3：小时聚合空时同样回退原始表，避免冷启动空白（复用既有回退语义）。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[], [_row(now)]])
        before_raw = _counter("raw")
        resp = _run_history(monkeypatch, conn, minutes=1440)

        assert len(conn.calls) == 2, "小时聚合为空时必须回退原始表"
        assert "robot_telemetry" in conn.calls[1]
        assert resp["points"][0]["joint_positions"] == {"left_knee_pitch": 0.5}
        assert _counter("raw") == before_raw + 1

    def test_hourly_columns_match_10s_columns(self):
        """小时路径与 10s 路径的 SELECT 列集**逐字一致**（bucket 表达式已中性化）。

        两视图结构镜像（init.sql §5.1b），一旦漂移，小时响应就会缺列。
        """
        hourly = _select_body(_capture_hourly_sql()["cagg"])
        ten_s = _select_body(_live_sqls()["cagg"])

        def _neutralize(body: str) -> str:
            return re.sub(r"^.*?\sAS bucket", "BUCKET_EXPR AS bucket", body, count=1)

        assert _neutralize(hourly) == _neutralize(ten_s), (
            "小时路径与 10s 路径列漂移：\n"
            f"  hourly: {hourly}\n  10s   : {ten_s}"
        )

    def test_hourly_branch_keeps_all_joint_columns(self):
        """三个 JSONB 关节列在小时路径（含回退）都必须在。"""
        for name, sql in _capture_hourly_sql().items():
            body = _select_body(sql)
            for col in JOINT_COLS:
                assert f"last({col}, time) AS {col}" in body, f"{name} 分支缺 {col}"

    def test_hourly_branch_reads_materialized_bucket(self):
        """小时路径 bucket 取物化列（不再套 time_bucket），才吃得到自动索引。"""
        sql = _capture_hourly_sql()["cagg"]
        assert "time_bucket" not in sql
        assert "bucket >= now()" in sql


class TestMinutesUpperBound:
    """R-12 T2 / V5：minutes 上限 400。"""

    def test_over_limit_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history(
                    "humanoid-001",
                    minutes=main_mod.MAX_HISTORY_MINUTES + 1,
                    resolution="10s",
                    _user=None,
                )
            )
        assert ei.value.status_code == 400
        assert ei.value.detail == "history span too large"

    def test_two_hundred_thousand_minutes_is_400(self):
        """需求 §5 V5 的口径：?minutes=20000 → 400（20000 > 10080）。"""
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=20000, resolution="10s", _user=None)
            )
        assert ei.value.status_code == 400
        assert ei.value.detail == "history span too large"

    def test_at_limit_is_not_rejected_by_bound(self, monkeypatch):
        """恰好等于上限（10080）不得 400 —— 边界必须是 >，不是 >=。"""
        now = _dt.datetime(2026, 10, 11, 3, 0, tzinfo=_dt.timezone.utc)
        conn = _FakeConn([[_row(now)]])
        resp = _run_history(monkeypatch, conn, minutes=main_mod.MAX_HISTORY_MINUTES)
        assert resp["resolution"] == "3600s"  # 10080 > 180 → 小时路径
        assert resp["points"]

    def test_bound_rejected_before_pool_acquired(self, monkeypatch):
        """上限校验必须在取连接之前 —— 否则全表扫描已经甩给 DB 才报 400。

        用一个「一旦 acquire 就断言失败」的池把顺序钉死。
        """
        from fastapi import HTTPException

        class _NoAcquirePool:
            def acquire(self):
                raise AssertionError("上限校验前不该取连接")

        from app.db import session as session_mod

        async def _fake():
            return _NoAcquirePool()

        monkeypatch.setattr(session_mod, "get_pool", _fake)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                main_mod.history("humanoid-001", minutes=20000, resolution="10s", _user=None)
            )
        assert ei.value.status_code == 400
