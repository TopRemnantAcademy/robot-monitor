"""Order 17 T3 — acceptance tests for silence and the three-layer filter order in emit().

Core assertion: **the order must be silence → inhibition → dedup** (work order T3 / conflict note).
Verification method: replace the three layers with probes, record the **actual call order**, and
verify "when silence is active, neither inhibition nor dedup is triggered" (verbatim acceptance
criterion).

We do not mock the logic under test itself — the assertion is the real control flow of
`app.main.emit()`.
"""

from __future__ import annotations

import pytest

from tests.conftest import run

from app.rules.base import Alert


def _alert(rid: str = "humanoid-001", rule_id: str = "battery_low",
           severity: int = 2) -> Alert:
    return Alert(robot_id=rid, rule_id=rule_id, severity=severity,
                 message=f"{rule_id} on {rid}", detail={})


@pytest.fixture()
def calls(engine, monkeypatch):
    """Replace the three layers with recorders and return the call-order list.

    - is_silenced  → return value controlled by test (default False)
    - is_inhibited → return value controlled by test (default False)
    - should_emit  → return value controlled by test (default True)
    - save         → records the final persisted event (already replaced by conftest)
    """
    order: list[str] = []
    state = {"silenced": False, "inhibited": False, "dedup_pass": True}

    async def probe_silence(alert):
        order.append("silence")
        return state["silenced"]

    def probe_inhibition(alert):
        order.append("inhibition")
        return state["inhibited"]

    async def probe_dedup(alert, window_s):
        order.append("dedup")
        return state["dedup_pass"]

    monkeypatch.setattr(engine, "is_silenced", probe_silence)
    monkeypatch.setattr(engine, "is_inhibited", probe_inhibition)
    monkeypatch.setattr(engine, "should_emit", probe_dedup)
    engine._order = order
    engine._state = state
    return engine


def test_order_is_silence_inhibition_dedup(calls):
    """When all three layers pass, the call order is strictly silence → inhibition → dedup."""
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition", "dedup"]
    assert len(calls.emitted) == 1


def test_silenced_skips_inhibition_and_dedup(calls):
    """Acceptance criterion: when silence is active, neither inhibition nor dedup is triggered."""
    calls._state["silenced"] = True
    run(calls.emit(_alert()))

    assert calls._order == ["silence"], "after a silence hit, inhibition/dedup must not be reached"
    assert "inhibition" not in calls._order
    assert "dedup" not in calls._order
    assert calls.emitted == [], "nothing must be persisted during the silence window"


def test_silenced_alert_logged_at_debug(calls, caplog):
    """Silently dropped alerts must leave a DEBUG trace (needed to investigate "why no alert")."""
    calls._state["silenced"] = True
    with caplog.at_level("DEBUG", logger="alert_engine"):
        run(calls.emit(_alert()))
    assert any("silenced" in r.message for r in caplog.records), caplog.text


def test_inhibited_still_runs_after_silence_pass(calls):
    """When silence misses but inhibition hits: only reach inhibition, not dedup."""
    calls._state["inhibited"] = True
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition"]
    assert calls.emitted == []


def test_dedup_runs_last(calls):
    """When the first two layers pass but dedup blocks: all three are called, but nothing persists."""
    calls._state["dedup_pass"] = False
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition", "dedup"]
    assert calls.emitted == []


# ------------------------------------------------ is_silenced semantics (real Redis key)

def test_is_silenced_hits_robot_key(monkeypatch):
    """A hit on `alert:silence:{robot_id}` means silenced."""
    import app.silence as silence

    class FakeRedis:
        def __init__(self, present):
            self._present = present
            self.calls = []

        async def mget(self, *keys):
            self.calls.append(keys)
            return ["1" if k in self._present else None for k in keys]

    fake = FakeRedis({"alert:silence:humanoid-007"})
    monkeypatch.setattr(silence, "get_redis", lambda: _await(fake))
    assert run(silence.is_silenced(_alert("humanoid-007"))) is True
    assert run(silence.is_silenced(_alert("humanoid-008"))) is False
    # Must MGET both the global and per-robot keys in a single round-trip
    assert fake.calls[0] == ("alert:silence:all", "alert:silence:humanoid-007")


def test_is_silenced_global_key(monkeypatch):
    import app.silence as silence

    class FakeRedis:
        async def mget(self, *keys):
            return ["1" if k == "alert:silence:all" else None for k in keys]

    monkeypatch.setattr(silence, "get_redis", lambda: _await(FakeRedis()))
    assert run(silence.is_silenced(_alert("humanoid-099"))) is True


def test_is_silenced_fail_open(monkeypatch, caplog):
    """Redis unreachable → fail-open (let the alert through); never suppress fault alerts
    because the silence layer itself is down."""
    import app.silence as silence

    class Boom:
        async def mget(self, *keys):
            raise RuntimeError("redis down")

    monkeypatch.setattr(silence, "get_redis", lambda: _await(Boom()))
    with caplog.at_level("ERROR", logger="silence"):
        assert run(silence.is_silenced(_alert())) is False


async def _await(obj):
    return obj
