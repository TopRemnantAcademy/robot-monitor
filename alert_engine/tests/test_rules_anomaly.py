"""AnomalyRule unit tests (stateless z-score detector)."""

from __future__ import annotations

from collections import deque

from app.config import settings
from app.rules.anomaly import AnomalyRule


def _window(values, robot_id="humanoid-001", joint="left_shoulder_pitch"):
    """values: list[float] — build battery_pct and the given joint temperature as the same time series."""
    w = deque(maxlen=settings.window_maxlen)
    for v in values:
        w.append(
            {
                "robot_id": robot_id,
                "battery_pct": v,
                "max_joint_temp": v,
                "joint_temperatures": {joint: v},
            }
        )
    return w


def test_no_alert_when_stable():
    # 50 stable values (~40 ± 0.3) → std is small, below anomaly_min_std or z not reached
    vals = [40.0 + 0.2 * (i % 3) for i in range(50)]
    rule = AnomalyRule()
    state = {
        "robot_id": "humanoid-001",
        "battery_pct": 40.3,
        "max_joint_temp": 40.3,
        "joint_temperatures": {"left_shoulder_pitch": 40.3},
    }
    assert rule.evaluate(state, _window(vals)) is None


def test_alert_on_sudden_spike():
    # Normal ~40 then the last sample jumps to 90 → z exceeds threshold
    vals = [40.0 + 0.2 * (i % 3) for i in range(49)] + [90.0]
    rule = AnomalyRule()
    state = {
        "robot_id": "humanoid-001",
        "battery_pct": 90.0,
        "max_joint_temp": 90.0,
        "joint_temperatures": {"left_shoulder_pitch": 90.0},
    }
    alert = rule.evaluate(state, _window(vals))
    assert alert is not None
    assert alert.rule_id == "anomaly"
    assert alert.severity == 2
    assert "z=" in alert.message
    assert abs(alert.detail["zscore"]) >= settings.anomaly_zscore


def test_no_alert_below_sample_threshold():
    # If the window is too small, evaluation is skipped entirely
    rule = AnomalyRule()
    small = deque(
        [{"robot_id": "humanoid-001", "battery_pct": 40.0 + i, "max_joint_temp": 40.0 + i,
           "joint_temperatures": {"left_shoulder_pitch": 40.0 + i}} for i in range(5)]
    )
    state = {"robot_id": "humanoid-001", "battery_pct": 99.0, "max_joint_temp": 99.0,
             "joint_temperatures": {"left_shoulder_pitch": 99.0}}
    assert rule.evaluate(state, small) is None


def test_alert_uses_joint_temperature_series():
    # Sudden jump only in a specific joint of joint_temperatures → the metric is that joint
    vals = [50.0 + 0.1 * (i % 2) for i in range(49)] + [120.0]
    rule = AnomalyRule()
    state = {
        "robot_id": "humanoid-001",
        "battery_pct": 50.0,
        "max_joint_temp": 120.0,
        "joint_temperatures": {"right_elbow_pitch": 120.0},
    }
    w = _window(vals, joint="right_elbow_pitch")
    alert = rule.evaluate(state, w)
    assert alert is not None
    assert alert.detail["metric"] == "joint_temperatures.right_elbow_pitch"
