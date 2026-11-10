"""Order 30 — severity-layer inhibition (higher severity on the same robot suppresses lower)
+ traceability acceptance tests.

Covers two hard requirements:
  A. **Higher severity suppresses same-robot lower severity**: when sev3 is active on a robot,
     sev2/sev1 are suppressed; equal levels don't suppress each other; cross-robot doesn't
     suppress; after the source recovers it auto-lifts.
  B. **Traceable, no silent drops**: every suppression produces an `InhibitionRecord` voucher,
     a WARNING log (with source/target/reason), and a standalone metric (with a reason dimension).

Assertions land on the real `is_inhibited()` / `inhibition_reason()` / `emit()` behavior, without
mocking the logic under test. The `engine` fixture (conftest) has already cleared active_alerts.
"""

from __future__ import annotations

import logging

import pytest

from tests.conftest import run

from app.config import settings
from app.inhibition import MIN_SOURCE_SEVERITY
from app.rules.base import Alert


def _alert(rid: str = "humanoid-001", rule_id: str = "battery_low",
           severity: int = 2) -> Alert:
    return Alert(robot_id=rid, rule_id=rule_id, severity=severity,
                 message=f"{rule_id} on {rid}", detail={})


# ================================================================ A. Semantics

def test_high_severity_inhibits_lower_on_same_robot(engine):
    """Core: same-robot motor_temp(sev3) active → battery_low(sev2) inhibited."""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))

    assert rec is not None, "sev3 should inhibit same-robot sev2"
    assert rec.reason == "severity"
    assert rec.source_rule == "motor_temp"
    assert rec.source_severity == 3
    assert rec.target_rule == "battery_low"
    assert engine.is_inhibited(_alert(rule_id="battery_low", severity=2)) is True


def test_equal_severity_not_inhibited(engine):
    """Equal levels don't suppress each other: two sev2 (battery_low / joint_stuck) don't suppress
    each other.

    Otherwise FIFO order would depend on dict iteration order — a race-condition miss."""
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="joint_stuck", severity=2)
    ) is None, "same severity must not suppress each other"


def test_lower_severity_does_not_inhibit_higher(engine):
    """The reverse doesn't hold: an active sev2 cannot suppress a sev3."""
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="motor_temp", severity=3)
    ) is None, "lower severity must not suppress higher"


def test_severity_inhibition_is_per_robot(engine):
    """Cross-robot doesn't suppress (equal: [robot_id]): A's high temp doesn't inhibit B's low battery."""
    engine.active_alerts.register(
        _alert(rid="humanoid-001", rule_id="motor_temp", severity=3)
    )

    rec = engine.inhibition_reason(
        _alert(rid="humanoid-002", rule_id="battery_low", severity=2)
    )
    assert rec is None, "inhibition must be limited to the same robot_id"


def test_source_below_min_severity_does_not_inhibit(engine):
    """The suppression source must be ≥ MIN_SOURCE_SEVERITY: sev2 is not allowed to suppress sev1
    (avoids an over-broad suppression surface)."""
    assert MIN_SOURCE_SEVERITY == 3, "this case locks the current threshold; change in sync"
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="info_thing", severity=1)
    ) is None


def test_lifted_when_source_recovers(engine):
    """After the source recovers (registration cleared) the inhibition auto-lifts and low-severity
    alerts fire again."""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert engine.emitted == [], "should be inhibited while the source is active"

    engine.active_alerts.clear("humanoid-001", "motor_temp")   # rule recovered
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_registry_keeps_highest(engine):
    """When multiple active alerts coexist, the registry takes the highest severity as the source."""
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    rec = engine.inhibition_reason(_alert(rule_id="joint_stuck", severity=2))
    assert rec is not None
    assert rec.source_rule == "motor_temp"
    assert rec.source_severity == 3


def test_switch_off_restores_old_behaviour(engine, monkeypatch):
    """With `inhibit_severity=False` behavior returns to before the suppression existed."""
    monkeypatch.setattr(settings, "inhibit_severity", False)
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    assert engine.is_inhibited(_alert(rule_id="battery_low", severity=2)) is False
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_offline_layer_unchanged(engine):
    """The offline layer semantics are unaffected (Order 10 regression): offline still suppresses
    same-robot derivative alerts."""
    engine.online_state["humanoid-001"] = False
    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))
    assert rec is not None and rec.reason == "offline"


# ================================================================ B. Traceability

def test_inhibition_emits_warning_log_with_evidence(engine, caplog):
    """Every suppression must log a WARNING containing source/target/reason (grep-able for audit)."""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    with caplog.at_level(logging.WARNING, logger="alert_engine"):
        run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("inhibited" in m for m in msgs), f"missing inhibition log: {msgs}"
    joined = " ".join(msgs)
    assert "reason=severity" in joined
    assert "source=motor_temp(sev3)" in joined
    assert "target=battery_low(sev2)" in joined
    assert "humanoid-001" in joined


def test_inhibition_increments_reason_labeled_metric(engine):
    """The suppression counter carries a reason dimension, attributable to severity (distinct from offline)."""
    from app import metrics
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    before = metrics.alerts_suppressed.labels(
        rule_id="battery_low", reason="severity"
    )._value.get()
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    after = metrics.alerts_suppressed.labels(
        rule_id="battery_low", reason="severity"
    )._value.get()

    assert after == before + 1, "severity suppression must count under reason=severity"


def test_inhibition_record_is_json_serialisable_evidence(engine):
    """The voucher is a structured object (no silent drop: machine-consumable evidence must remain)."""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))

    assert rec is not None
    assert rec.robot_id == "humanoid-001"
    assert rec.ts_ms > 0
    fields = rec.as_log_fields()
    for token in ("reason=severity", "source=motor_temp", "target=battery_low"):
        assert token in fields


def test_suppressed_alert_not_saved_nor_registered(engine):
    """A suppressed alert is neither persisted nor registered as an active alert (no pollution of
    later judgements)."""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    assert engine.emitted == []
    assert "battery_low" not in engine.active_alerts.snapshot()["humanoid-001"]


# ================================================================ C. Persistence closed-loop (Order 35)

def test_emit_persists_inhibition_record(engine, monkeypatch):
    """When inhibited, `emit()` must call `save_suppressed` (persist the voucher, order 35).

    Order 30 only got as far as "log + metric"; `GET /alerts` couldn't find the suppressed row.
    This nails the wiring: the suppression branch must spawn a persist once — and it must **not**
    be `save()` (the latter would PUBLISH, effectively degrading suppression back into a pass)."""
    import app.main as m

    persisted: list = []

    async def fake_save_suppressed(alert, record):
        persisted.append((alert, record))

    monkeypatch.setattr(m, "save_suppressed", fake_save_suppressed)

    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    assert engine.emitted == [], "a suppressed alert must not go through save()/publish"
    assert len(persisted) == 1, "the suppression branch must persist once (order 35)"
    alert, record = persisted[0]
    assert alert.rule_id == "battery_low"
    assert record.source_rule == "motor_temp"
    assert record.reason == "severity"


def test_emit_does_not_persist_when_not_inhibited(engine, monkeypatch):
    """Reverse guard: a passed-through alert must not *also* produce a suppression record
    (otherwise the DB would double-count)."""
    import app.main as m

    persisted: list = []

    async def fake_save_suppressed(alert, record):
        persisted.append((alert, record))

    monkeypatch.setattr(m, "save_suppressed", fake_save_suppressed)

    run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    assert [a.rule_id for a in engine.emitted] == ["battery_low"]
    assert persisted == [], "a non-inhibited alert must not produce a suppression record"
