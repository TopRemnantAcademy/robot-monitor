"""Order 35 —— suppressed alerts **must be persisted** (`store.save_suppressed`).

Why this test is the backend core of Order 35
----------------------------------
Order 30 gave the suppression judgement and the `InhibitionRecord` voucher, but it only produced
up to **logs and metrics**: once an alert was suppressed it `return`ed and never wrote to the DB.
So `GET /alerts` had no such row at all — "don't silently drop" was not closed at the REST layer;
ops could only answer "why didn't this one go out" inside the log system, not on the alert board.

This file nails down three things (each a "degrades if written wrong" point):
  1. The suppressed alert **enters the DB**, with `suppressed=TRUE` + `suppressed_by=<root-cause rule>`;
  2. It does **not** PUBLISH to `robot:alerts` — the semantics of suppression is "don't push to the
     live dashboard stream"; if it were also published, the suppression layer would visually not exist;
  3. `detail` carries the full forensic fields (reason / severity comparison / ts), so "why was it
     suppressed" doesn't require digging through logs again.

The object under test is the real `store.save_suppressed`; only the connection entry points are
swapped for in-memory recorders — the fake's logic is not asserted (same discipline as
test_store_metrics.py).
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
    """Records (sql, args) and back-fills an auto-increment id in the SUPPRESSED_INSERT_SQL column order."""

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
    """Publish is recorded — this file uses it for the **reverse** assertion (must never be sent)."""

    def __init__(self, sent: list):
        self._sent = sent

    async def publish(self, chan, payload):
        self._sent.append((chan, payload))


@pytest.fixture()
def store_env(monkeypatch):
    """Swap `app.store`'s pool and Redis for in-memory ones; return (module, sql_sink, pub_sink)."""
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
    """The suppressed one: battery_low(sev2), with motor_temp(sev3) active on the same robot."""
    return Alert(
        robot_id="humanoid-001",
        rule_id="battery_low",
        severity=2,
        message="Battery 17.4% below 20%",
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


# ---------------------------------------------------------- 1. persistence

def test_suppressed_alert_is_persisted(store_env):
    """Core: the suppression record is written to robot_alert, with both suppression markers."""
    _store, sql_sink, _pub = store_env
    run(save_suppressed(_alert(), _record()))

    assert len(sql_sink) == 1, "a suppressed alert must persist exactly once"
    sql, args = sql_sink[0]
    # Assert semantics, not the entire SQL text equality: full-text equality would go red on a
    # comment or whitespace change.
    assert "INSERT INTO robot_alert" in sql
    assert "suppressed, suppressed_by" in sql, "INSERT must write both suppression marker columns explicitly"
    assert "TRUE" in sql, "suppressed must be set TRUE by SQL (not rely on the column default)"

    # Argument position order matches the column order
    # (robot_id, rule_id, severity, message, detail, suppressed_by)
    assert args[1] == "humanoid-001"
    assert args[2] == "battery_low"
    assert args[3] == 2
    assert args[4] == "Battery 17.4% below 20%"
    assert args[6] == "motor_temp", "suppressed_by must be the suppression source rule"


def test_suppressed_alert_does_not_publish(store_env):
    """A suppressed item does **not** publish to robot:alerts — otherwise the suppression layer
    visually does not exist.

    This is the only mechanized guarantee of "don't push to the live dashboard stream": `save()`
    PUBLISHes while `save_suppressed()` does not. The moment the two are merged (reusing `save()`
    for convenience), this goes red immediately."""
    _store, _sql, pub_sink = store_env
    run(save_suppressed(_alert(), _record()))
    assert pub_sink == [], "a suppressed alert must not be published to robot:alerts"


def test_detail_carries_forensic_fields(store_env):
    """detail carries the full forensic fields: reason / severity comparison / ts — no log dig needed
    afterwards."""
    _store, sql_sink, _pub = store_env
    run(save_suppressed(_alert(), _record()))

    detail = json.loads(sql_sink[0][1][5])
    # The rule's own detail must be preserved (`||` merge, not overwrite)
    assert detail["battery_pct"] == 17.4
    # Suppression voucher
    assert detail["suppressed_reason"] == "severity"
    assert detail["suppressed_source_rule"] == "motor_temp"
    assert detail["suppressed_source_severity"] == 3
    assert detail["suppressed_target_severity"] == 2
    assert detail["suppressed_ts_ms"] == 1760000000000


# ---------------------------------------------------------- 2. failures observable

def test_insert_failure_increments_store_errors(monkeypatch):
    """A persistence failure must be counted — same discipline as `save()`, otherwise "not in DB"
    and "no such alert" are indistinguishable."""
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

    run(save_suppressed(_alert(), _record()))          # must not raise (bypass semantics)

    text = generate_latest(metrics.registry).decode()
    m = re.search(r"^robot_alert_store_errors_total ([0-9.eE+-]+)$", text, re.M)
    after = float(m.group(1)) if m else 0.0
    assert after == before + 1, "save_suppressed failure not counted in store_errors"
