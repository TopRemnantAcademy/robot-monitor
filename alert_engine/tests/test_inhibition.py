"""订单 10 — 告警抑制（inhibition）验收测试。

覆盖 issue #13 §4 要求的 5 个必测场景，外加 3 个边界场景。
全部断言落在 `OnlineState` + `emit()` 的真实行为上，不 mock 被测逻辑本身。
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


# ------------------------------------------------ 必测 5 场景

def test_offline_active_inhibits_battery_low(engine):
    """场景 1：机器人离线时，battery_low 被抑制且不落库。"""
    engine.online_state["humanoid-001"] = False          # ticker 已判离线
    run(engine.emit(_alert()))

    assert engine.emitted == [], "离线时 battery_low 不应落库"


def test_offline_rule_never_inhibited(engine):
    """场景 2：offline 规则自身永不被抑制（否则根因告警永远报不出来）。"""
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert(rule_id="offline", severity=3)))

    assert [a.rule_id for a in engine.emitted] == ["offline"]

    # 直接验证判定函数，写死这条不变量
    assert engine.is_inhibited(_alert(rule_id="offline", severity=3)) is False


def test_inhibition_lifted_after_recovery(engine):
    """场景 3：机器人恢复在线后，抑制自动解除，battery_low 重新发出。"""
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert()))
    assert engine.emitted == []                          # 离线期间被抑制

    engine.online_state["humanoid-001"] = True           # ticker 判恢复
    run(engine.emit(_alert()))
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_unseen_robot_not_inhibited(engine):
    """场景 4：`online_state` 无该 robot（刚启动）时**不抑制**。

    这是本单最关键的陷阱：ticker 跑满一轮前字典是空的，
    若用 `not online_state.get(rid)` 会把所有告警全吞掉。
    """
    assert "humanoid-042" not in engine.online_state      # 前提：字典无记录
    run(engine.emit(_alert(rid="humanoid-042")))

    assert [a.rule_id for a in engine.emitted] == ["battery_low"]
    assert engine.is_inhibited(_alert(rid="humanoid-042")) is False


def test_switch_off_restores_old_behaviour(engine, monkeypatch):
    """场景 5：`inhibit_offline=False` 时行为与加抑制之前一致。"""
    from app.config import settings
    monkeypatch.setattr(settings, "inhibit_offline", False)

    engine.online_state["humanoid-001"] = False          # 明明离线
    run(engine.emit(_alert()))

    assert [a.rule_id for a in engine.emitted] == ["battery_low"], \
        "关闭开关后不应抑制"


# ------------------------------------------------ 边界场景

@pytest.mark.parametrize("rule_id,severity", [
    ("motor_temp", 3),
    ("battery_low", 2),
    ("joint_stuck", 2),
])
def test_all_stream_rules_inhibited(engine, rule_id, severity):
    """离线时，消费线三条规则**全部**被抑制（不只是 battery_low）。"""
    engine.online_state["humanoid-007"] = False
    run(engine.emit(_alert(rid="humanoid-007", rule_id=rule_id, severity=severity)))

    assert engine.emitted == []


def test_inhibition_is_per_robot(engine):
    """抑制按 robot_id 隔离：A 离线不影响 B。"""
    engine.online_state["humanoid-001"] = False
    engine.online_state["humanoid-002"] = True

    run(engine.emit(_alert(rid="humanoid-001")))
    run(engine.emit(_alert(rid="humanoid-002")))

    assert [(a.robot_id, a.rule_id) for a in engine.emitted] == \
        [("humanoid-002", "battery_low")]


def test_inhibited_alert_logged_at_debug(engine, caplog):
    """被抑制的告警必须有 DEBUG 日志，不能静默丢弃。"""
    engine.online_state["humanoid-001"] = False

    with caplog.at_level(logging.DEBUG, logger="alert_engine"):
        run(engine.emit(_alert()))

    msgs = [r.getMessage() for r in caplog.records]
    assert any("inhibited" in m and "humanoid-001" in m for m in msgs), \
        f"缺少抑制 DEBUG 日志，实际: {msgs}"


def test_dedup_runs_after_inhibition(engine, monkeypatch):
    """抑制是**前置**于去重的一层：被抑制时不应触碰 deduplicator。"""
    calls: list = []

    async def spy_should_emit(alert, window_s):
        calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "should_emit", spy_should_emit)
    engine.online_state["humanoid-001"] = False
    run(engine.emit(_alert()))

    assert calls == [], "被抑制的告警不应进入去重逻辑"

    engine.online_state["humanoid-001"] = True
    run(engine.emit(_alert()))
    assert calls == ["battery_low"], "未抑制时应正常走去重"
