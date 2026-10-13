"""订单 35 —— 被抑制告警**必须落库**（`store.save_suppressed`）。

为什么这条测试是订单 35 的后端核心
----------------------------------
订单 30 给出了抑制的判定与 `InhibitionRecord` 凭证，但它只产出到**日志与指标**为止：
告警一旦被抑制就 `return`，从不写库。于是 `GET /alerts` 里根本没有这行 ——
「不静默丢弃」在 REST 层面并未闭环，运维只能在日志系统里回答
「这条为什么没出去」，告警台上看不到被压住的那条。

本文件钉住三件事（每一条都是"写错就退化"的点）：
  1. 被抑制的告警**进了库**，且 `suppressed=TRUE` + `suppressed_by=根因规则`；
  2. 它**不发** `robot:alerts` —— 抑制的语义就是"不推给看板实时流"，
     若也发出去，抑制层在观感上等于不存在；
  3. `detail` 里带齐取证字段（reason / severity 对比 / ts），
     使"为什么被压"不必再翻日志。

被测对象是真 `store.save_suppressed`，只把连接入口换成内存记录器 ——
不断言替身逻辑（与 test_store_metrics.py 同一纪律）。
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import run

from app import metrics
from app.inhibition import InhibitionRecord
from app.rules.base import Alert
from app.store import save_suppressed


class _RecordingConn:
    """记下 (sql, args)，并按 SUPPRESSED_INSERT_SQL 的列序回填一个自增 id。"""

    def __init__(self, sink: list):
        self._sink = sink

    async def fetchval(self, sql, *args):
        self._sink.append((sql, args))
        return 4242


class _Ctx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, sink: list):
        self._sink = sink

    def acquire(self):
        return _Ctx(_RecordingConn(self._sink))


class _GhostRedis:
    """发布会被记录下来 —— 本文件用它做**反向**断言（必须一次都没发）。"""

    def __init__(self, sent: list):
        self._sent = sent

    async def publish(self, chan, payload):
        self._sent.append((chan, payload))


@pytest.fixture()
def store_env(monkeypatch):
    """把 `app.store` 的池与 Redis 换成内存件，返回 (module, sql_sink, pub_sink)。"""
    import app.store as store

    sql_sink: list = []
    pub_sink: list = []

    async def fake_get_pool():
        return _Pool(sql_sink)

    async def fake_get_redis():
        return _GhostRedis(pub_sink)

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)
    return store, sql_sink, pub_sink


def _alert() -> Alert:
    """被压住的那条：battery_low(sev2)，同机有 motor_temp(sev3) 在活跃。"""
    return Alert(
        robot_id="humanoid-001",
        rule_id="battery_low",
        severity=2,
        message="电量 17.4% 低于 20%",
        detail={"battery_pct": 17.4},
    )


def _record() -> InhibitionRecord:
    return InhibitionRecord(
        robot_id="humanoid-001",
        target_rule="battery_low",
        target_severity=2,
        source_rule="motor_temp",
        source_severity=3,
        reason="severity",
        ts_ms=1760000000000,
    )


# ---------------------------------------------------------- 1. 落库

def test_suppressed_alert_is_persisted(store_env):
    """核心：抑制记录写进 robot_alert，且有 suppressed / suppressed_by 两个标记。"""
    _store, sql_sink, _pub = store_env
    run(save_suppressed(_alert(), _record()))

    assert len(sql_sink) == 1, "被抑制告警必须落库一次"
    sql, args = sql_sink[0]
    # 断言语义而不是整段 SQL 文本相等：整段相等会让改注释/换行就红。
    assert "INSERT INTO robot_alert" in sql
    assert "suppressed, suppressed_by" in sql, "INSERT 必须显式写两个抑制标记列"
    assert "TRUE" in sql, "suppressed 必须由 SQL 置 TRUE（不是靠列默认值）"

    # 参数位序与列序一一对应（robot_id, rule_id, severity, message, detail, suppressed_by）
    assert args[1] == "humanoid-001"
    assert args[2] == "battery_low"
    assert args[3] == 2
    assert args[4] == "电量 17.4% 低于 20%"
    assert args[6] == "motor_temp", "suppressed_by 必须是抑制源规则"


def test_suppressed_alert_does_not_publish(store_env):
    """抑制项**不发** robot:alerts —— 否则抑制层在观感上等于不存在。

    这条是「不推给看板实时流」的唯一机器化保证：`save()` 会 PUBLISH，
    `save_suppressed()` 不会。两者一旦被合并（图省事复用 `save()`），
    这里立刻红。
    """
    _store, _sql, pub_sink = store_env
    run(save_suppressed(_alert(), _record()))
    assert pub_sink == [], "被抑制告警不得发布到 robot:alerts"


def test_detail_carries_forensic_fields(store_env):
    """detail 里带齐取证字段：reason / severity 对比 / ts —— 事后无需翻日志。"""
    _store, sql_sink, _pub = store_env
    run(save_suppressed(_alert(), _record()))

    detail = json.loads(sql_sink[0][1][5])
    # 规则自带的 detail 必须保留（`||` 合并而非覆盖）
    assert detail["battery_pct"] == 17.4
    # 抑制凭证
    assert detail["suppressed_reason"] == "severity"
    assert detail["suppressed_source_rule"] == "motor_temp"
    assert detail["suppressed_source_severity"] == 3
    assert detail["suppressed_target_severity"] == 2
    assert detail["suppressed_ts_ms"] == 1760000000000


# ---------------------------------------------------------- 2. 失败可观测

def test_insert_failure_increments_store_errors(monkeypatch):
    """落库失败必须计数 —— 与 `save()` 同一纪律，否则"库里没有"与"没这条告警"不可分辨。"""
    import re

    import app.store as store

    class BrokenPool:
        def acquire(self):
            raise ConnectionError("injected: pool exhausted")

    async def fake_get_pool():
        return BrokenPool()

    monkeypatch.setattr(store, "get_pool", fake_get_pool)

    from prometheus_client import generate_latest

    text = generate_latest(metrics.registry).decode()
    m = re.search(r"^robot_alert_store_errors_total ([0-9.eE+-]+)$", text, re.M)
    before = float(m.group(1)) if m else 0.0

    run(save_suppressed(_alert(), _record()))          # 必须不抛（旁路语义）

    text = generate_latest(metrics.registry).decode()
    m = re.search(r"^robot_alert_store_errors_total ([0-9.eE+-]+)$", text, re.M)
    after = float(m.group(1)) if m else 0.0
    assert after == before + 1, "save_suppressed 失败未计入 store_errors"
