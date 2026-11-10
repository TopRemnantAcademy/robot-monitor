"""G-4: alert persistence failures **must be observable** (`robot_alert_store_errors_total`).

The object under test is the real exception branch of `app/store.py`, not fake logic: feed a real
`Alert` into the real `save()`, only swapping the connection entry point for a pool that throws,
then read the current value of the Counter from the **real registry**.

Why not read `metrics.store_errors._value.get()` directly: that's a private API (0.21 and 0.26
take different paths to the value), and the test would become "pinning the py-library internals".
`generate_latest(metrics.registry)` is the public contract and also incidentally verifies "the
metric really can be scraped by Prometheus" — if you only assert internal variables and not the
exposure side, a wrong `registry=` would still go all green while scraping nothing online.
(`alert_engine/app/metrics.py` has no `render()`: unlike the backend it's not mounted on a
FastAPI route but calls `generate_latest(registry)` directly inside `_Handler.do_GET`.)

The reverse guard at the bottom of this file, `test_guard_detects_a_store_that_forgets_to_count`:
revert store.py to the old "log only, no count" version and the cases above must go red.
Otherwise this set of cases is just applauding an assertion that can never fail.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import run

from prometheus_client import generate_latest

from app import metrics
from app.rules.base import Alert
from app.store import save

METRIC = "robot_alert_store_errors_total"


def _value(text: str, name: str) -> float:
    """Pull the current value of a label-less metric from the Prometheus text-exposure format
    (treat absent as 0)."""
    m = re.search(rf"^{name} ([0-9.eE+-]+)$", text, re.M)
    return float(m.group(1)) if m else 0.0


def _stored() -> float:
    return _value(generate_latest(metrics.registry).decode(), METRIC)


def _alert() -> Alert:
    return Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                 message="Joint 3 temperature 82.1°C exceeds 75°C", detail={"joint": 3})


class _BrokenPool:
    """`pool.acquire()` throws, simulating connection-pool exhaustion / network interruption.

    Deliberately uses an exception at the **connection-acquisition stage** rather than the
    `conn.execute` stage: inside `save()` the acquire is within try, and both paths must be counted."""

    def acquire(self):
        raise ConnectionError("injected: pool exhausted")


class _GhostRedis:
    """Publish succeeds but doesn't really connect to Redis (this case only cares about the persistence branch)."""

    def __init__(self, sent: list):
        self._sent = sent

    async def publish(self, chan, payload):
        self._sent.append((chan, payload))


@pytest.fixture()
def broken_store(monkeypatch):
    """Swap the two entry points of `app.db` for fakes (conftest has already stubbed the whole app.db module)."""
    import app.store as store

    sent: list = []

    async def fake_get_pool():
        return _BrokenPool()

    async def fake_get_redis():
        return _GhostRedis(sent)

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)
    return store, sent


# ------------------------------------------------- 1. failures must be counted

def test_insert_failure_increments_counter(broken_store):
    """Core assertion: one persistence failure → counter +1, and the metric name is visible in
    the /metrics text."""
    before = _stored()
    run(save(_alert()))
    assert _stored() == before + 1, f"{METRIC} did not increase with a persistence failure"


def test_counter_is_monotonic_across_failures(broken_store):
    """Repeated failures must accumulate (a Counter, not a "did a failure ever happen" boolean)."""
    before = _stored()
    for _ in range(3):
        run(save(_alert()))
    assert _stored() == before + 3


def test_failure_still_publishes_to_frontend(broken_store):
    """A persistence failure must **not** choke the frontend alert stream: the two try blocks are
    independent.

    What this nails is "don't swallow the publish too while fixing G-4" — the on-call must still
    see the alert on the dashboard even if it can't enter the DB this instant."""
    _store, sent = broken_store
    run(save(_alert()))
    assert len(sent) == 1
    chan, payload = sent[0]
    assert chan == "robot:alerts"
    assert '"rule_id":"motor_temp"' in payload


# ------------------------------------------------- 2. success must not count

def test_success_does_not_increment_counter(monkeypatch):
    """Reverse assertion: on a successful write the counter must stay untouched (otherwise rules
    would false-alarm every day)."""
    import app.store as store

    class OkConn:
        async def execute(self, *a, **kw):
            return "INSERT 0 1"

        async def fetchval(self, *a, **kw):
            # store.py:42 uses RETURNING alert_id to fetch the auto-increment PK; the success path
            # must support it, otherwise AttributeError gets swallowed by except as "persistence
            # failed" → this case's reverse guard goes red.
            return 1

    class OkPool:
        def acquire(self):
            return _AsyncCtx(OkConn())

    async def fake_get_pool():
        return OkPool()

    async def fake_get_redis():
        return _GhostRedis([])

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)

    before = _stored()
    run(save(_alert()))
    assert _stored() == before, "success path must not increment the persistence-failure counter"


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


# ------------------------------------------------- 3. the rule file still references the metric (guard against later deletion)

def test_rule_file_references_the_metric_with_increase():
    """The rule file must use increase(...[5m]) rather than `> 0` (a Counter is monotonic; the
    latter never resets)."""
    from pathlib import Path

    rules = (Path(__file__).resolve().parents[2]
             / "infra/observability/rules/robot-alerts.yml").read_text(encoding="utf-8")
    assert f"increase({METRIC}[5m]) > 0" in rules


# ------------------------------------------------- 4. reverse guard

def test_guard_detects_a_store_that_forgets_to_count(monkeypatch):
    """Revert store.py to the old "log only, no count" version and the counting cases must go red.

    Method: monkeypatch `metrics.store_errors.inc` into a no-op — equivalent to the old code
    missing the counter entirely. If this file's cases actually have teeth, `save()`'s failure
    should leave the value **unchanged** (i.e. the old behavior). This is also a meta-test: it
    proves the cases above really watch "is there an inc()", not some incidental change."""
    import app.store as store

    async def fake_get_pool():
        return _BrokenPool()

    async def fake_get_redis():
        return _GhostRedis([])

    monkeypatch.setattr(store, "get_pool", fake_get_pool)
    monkeypatch.setattr(store, "get_redis", fake_get_redis)
    monkeypatch.setattr(metrics.store_errors, "inc", lambda *a, **kw: None)

    before = _stored()
    run(save(_alert()))
    assert _stored() == before, "counting still happens after the counter was removed — assertion isn't on the counter"
