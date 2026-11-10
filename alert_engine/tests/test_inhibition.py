"""Order 10 — acceptance tests for alert inhibition.

Covers the 5 mandatory scenarios from issue #13 §4, plus 3 boundary scenarios.
All assertions land on the real behavior of `OnlineState` + `emit()`, without mocking the logic
under test itself.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import run

from app.rules.base import Alert


def _alert(rid: str = "humanoid-001", rule_id: str = "battery_low",
           severity: int = 2) -> Alert:
    return Alert(robot_id=rid, rule_id=rule_id, severity=severity,
                 message=f"{rule_id} on {rid}", detail={})


# ------------------------------------------------ 5 mandatory scenarios

def test_offline_active_inhibits_battery_low(engine):
    """Scenario 1: when a robot is offline, battery_low is inhibited and not persisted."""
    engine.online_state["humanoid-001"] = False          # ticker already judged offline
    run(engine.emit(_alert()))

    assert engine.emitted == [], "battery_low must not persist while offline"


def test_offline_rule_never_inhibited(engine):
    """Scenario 2: the offline rule itself is never inhibited (otherwise the root-cause alert
    would never surface)."""
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert(rule_id="offline", severity=3)))

    assert [a.rule_id for a in engine.emitted] == ["offline"]

    # Verify the judgement function directly, hard-coding this invariant
    assert engine.is_inhibited(_alert(rule_id="offline", severity=3)) is False


def test_inhibition_lifted_after_recovery(engine):
    """Scenario 3: after the robot recovers online, inhibition is lifted and battery_low fires again."""
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert()))
    assert engine.emitted == []                          # inhibited while offline

    engine.online_state["humanoid-001"] = True           # ticker judged recovery
    run(engine.emit(_alert()))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_unseen_robot_not_inhibited(engine):
    """Scenario 4: when `online_state` has no record for the robot (just started) it is **not**
    inhibited.

    This is the most critical trap of this order: before the ticker has run a full round the dict
    is empty; using `not online_state.get(rid)` would swallow every alert."""
    assert "humanoid-042" not in engine.online_state      # precondition: no record
    run(engine.emit(_alert(rid="humanoid-042")))

    assert [a.rule_id for a in engine.emitted] == ["battery_low"]
    assert engine.is_inhibited(_alert(rid="humanoid-042")) is False


def test_switch_off_restores_old_behaviour(engine, monkeypatch):
    """Scenario 5: with `inhibit_offline=False` the behavior matches before inhibition existed."""
    from app.config import settings
    monkeypatch.setattr(settings, "inhibit_offline", False)

    engine.online_state["humanoid-001"] = False          # clearly offline
    run(engine.emit(_alert()))

    assert [a.rule_id for a in engine.emitted] == ["battery_low"], \
        "nothing should be inhibited when the switch is off"


# ------------------------------------------------ boundary scenarios

@pytest.mark.parametrize("rule_id,severity", [
    ("motor_temp", 3),
    ("battery_low", 2),
    ("joint_stuck", 2),
])
def test_all_stream_rules_inhibited(engine, rule_id, severity):
    """When offline, **all three** stream rules are inhibited (not just battery_low)."""
    engine.online_state["humanoid-007"] = False
    run(engine.emit(_alert(rid="humanoid-007", rule_id=rule_id, severity=severity)))

    assert engine.emitted == []


def test_inhibition_is_per_robot(engine):
    """Inhibition is isolated by robot_id: A offline does not affect B."""
    engine.online_state["humanoid-001"] = False
    engine.online_state["humanoid-002"] = True

    run(engine.emit(_alert(rid="humanoid-001")))
    run(engine.emit(_alert(rid="humanoid-002")))

    assert [(a.robot_id, a.rule_id) for a in engine.emitted] == \
        [("humanoid-002", "battery_low")]


def test_inhibited_alert_logged_at_debug(engine, caplog):
    """Inhibited alerts must produce a DEBUG log; they must not be silently dropped."""
    engine.online_state["humanoid-001"] = False

    with caplog.at_level(logging.DEBUG, logger="alert_engine"):
        run(engine.emit(_alert()))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("inhibited" in m and "humanoid-001" in m for m in msgs), \
        f"missing inhibition DEBUG log, actual: {msgs}"


def test_dedup_runs_after_inhibition(engine, monkeypatch):
    """Inhibition is a layer **preceding** dedup: when inhibited, the deduplicator must not be touched."""
    calls: list = []

    async def spy_should_emit(alert, window_s):
        calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "should_emit", spy_should_emit)
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert()))

    assert calls == [], "an inhibited alert must not enter the dedup logic"

    engine.online_state["humanoid-001"] = True
    run(engine.emit(_alert()))
    assert calls == ["battery_low"], "when not inhibited, dedup should run normally"
