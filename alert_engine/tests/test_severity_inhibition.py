"""订单 30 — severity 层抑制（高等级抑制同机低等级）+ 可追溯性验收测试。

覆盖两条硬要求：
  A. **高等级抑制同机低等级**：同机 sev3 活跃时，sev2/sev1 被抑制；
     同级不互压；跨机不互压；源恢复后自动解除。
  B. **可追溯，禁止静默丢弃**：每次抑制都产出 `InhibitionRecord` 凭证、
     WARNING 日志（含 source/target/reason）、独立指标（带 reason 维度）。

断言落在真实的 `is_inhibited()` / `inhibition_reason()` / `emit()` 行为上，
不 mock 被测逻辑本身。`engine` fixture（conftest）已清空 active_alerts。
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


# ================================================================ A. 语义

def test_high_severity_inhibits_lower_on_same_robot(engine):
    """核心：同机 motor_temp(sev3) 活跃 → battery_low(sev2) 被抑制。"""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))

    assert rec is not None, "sev3 应抑制同机 sev2"
    assert rec.reason == "severity"
    assert rec.source_rule == "motor_temp"
    assert rec.source_severity == 3
    assert rec.target_rule == "battery_low"
    assert engine.is_inhibited(_alert(rule_id="battery_low", severity=2)) is True


def test_equal_severity_not_inhibited(engine):
    """同级不互压：两颗 sev2（battery_low / joint_stuck）互不抑制。

    否则先进先出取决于字典序，是竞态误伤。
    """
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="joint_stuck", severity=2)
    ) is None, "同 severity 不应互相抑制"


def test_lower_severity_does_not_inhibit_higher(engine):
    """反向不成立：sev2 活跃不能压制 sev3。"""
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="motor_temp", severity=3)
    ) is None, "低等级不得抑制高等级"


def test_severity_inhibition_is_per_robot(engine):
    """跨机不互压（equal: [robot_id]）：A 机高温不抑制 B 机低电量。"""
    engine.active_alerts.register(
        _alert(rid="humanoid-001", rule_id="motor_temp", severity=3)
    )

    rec = engine.inhibition_reason(
        _alert(rid="humanoid-002", rule_id="battery_low", severity=2)
    )
    assert rec is None, "抑制必须限定同一 robot_id"


def test_source_below_min_severity_does_not_inhibit(engine):
    """抑制源必须 ≥ MIN_SOURCE_SEVERITY：sev2 不配压制 sev1（避免抑制面过宽）。"""
    assert MIN_SOURCE_SEVERITY == 3, "本用例锁死当前阈值，改动需同步此断言"
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))

    assert engine.inhibition_reason(
        _alert(rule_id="info_thing", severity=1)
    ) is None


def test_lifted_when_source_recovers(engine):
    """源恢复（登记被 clear）后抑制自动解除，低等级告警重新发出。"""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert engine.emitted == [], "源活跃期间应被抑制"

    engine.active_alerts.clear("humanoid-001", "motor_temp")   # 规则恢复
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_registry_keeps_highest(engine):
    """登记簿在多条活跃告警并存时，取最高 severity 作为抑制源。"""
    engine.active_alerts.register(_alert(rule_id="battery_low", severity=2))
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    rec = engine.inhibition_reason(_alert(rule_id="joint_stuck", severity=2))
    assert rec is not None
    assert rec.source_rule == "motor_temp"
    assert rec.source_severity == 3


def test_switch_off_restores_old_behaviour(engine, monkeypatch):
    """`inhibit_severity=False` 时行为回到加抑制之前。"""
    monkeypatch.setattr(settings, "inhibit_severity", False)
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    assert engine.is_inhibited(_alert(rule_id="battery_low", severity=2)) is False
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_offline_layer_unchanged(engine):
    """offline 层语义不受影响（订单 10 回归）：离线仍抑制同机衍生告警。"""
    engine.online_state["humanoid-001"] = False
    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))
    assert rec is not None and rec.reason == "offline"


# ================================================================ B. 可追溯

def test_inhibition_emits_warning_log_with_evidence(engine, caplog):
    """每次抑制必须打 WARNING 日志，含 source/target/reason（可 grep 追溯）。"""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    with caplog.at_level(logging.WARNING, logger="alert_engine"):
        run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("inhibited" in m for m in msgs), f"缺少抑制日志: {msgs}"
    joined = " ".join(msgs)
    assert "reason=severity" in joined
    assert "source=motor_temp(sev3)" in joined
    assert "target=battery_low(sev2)" in joined
    assert "humanoid-001" in joined


def test_inhibition_increments_reason_labeled_metric(engine):
    """抑制计数带 reason 维度，可归因到 severity（区别于 offline）。"""
    from app import metrics
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))

    before = metrics.alerts_suppressed.labels(
        rule_id="battery_low", reason="severity"
    )._value.get()
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))
    after = metrics.alerts_suppressed.labels(
        rule_id="battery_low", reason="severity"
    )._value.get()

    assert after == before + 1, "severity 抑制必须计入 reason=severity 指标"


def test_inhibition_record_is_json_serialisable_evidence(engine):
    """凭证是结构化对象（禁止静默丢弃：必须留下可机器消费的证据）。"""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    rec = engine.inhibition_reason(_alert(rule_id="battery_low", severity=2))

    assert rec is not None
    assert rec.robot_id == "humanoid-001"
    assert rec.ts_ms > 0
    fields = rec.as_log_fields()
    for token in ("reason=severity", "source=motor_temp", "target=battery_low"):
        assert token in fields


def test_suppressed_alert_not_saved_nor_registered(engine):
    """被抑制的告警既不落库，也不登记为活跃告警（不污染后续判据）。"""
    engine.active_alerts.register(_alert(rule_id="motor_temp", severity=3))
    run(engine.emit(_alert(rule_id="battery_low", severity=2)))

    assert engine.emitted == []
    assert "battery_low" not in engine.active_alerts.snapshot()["humanoid-001"]
