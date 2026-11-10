"""AnomalyRule 단위 테스트 (stateless z-score detector)."""

from __future__ import annotations

from collections import deque

from app.config import settings
from app.rules.anomaly import AnomalyRule


def _window(values, robot_id="humanoid-001", joint="left_shoulder_pitch"):
    """values: list[float] — battery_pct 와 해당 joint 온도를 동일 시계열로 구성."""
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
    # 50 개의 안정적 값 (~40 ± 0.3) -> std 가 작아 anomaly_min_std 미만이거나 z 미도달
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
    # 정상 ~40 에서 마지막 샘플이 90 으로 돌변 -> z 가 임계 초과
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
    # window 가 너무 작으면 평가 자체를 하지 않음
    rule = AnomalyRule()
    small = deque(
        [{"robot_id": "humanoid-001", "battery_pct": 40.0 + i, "max_joint_temp": 40.0 + i,
           "joint_temperatures": {"left_shoulder_pitch": 40.0 + i}} for i in range(5)]
    )
    state = {"robot_id": "humanoid-001", "battery_pct": 99.0, "max_joint_temp": 99.0,
             "joint_temperatures": {"left_shoulder_pitch": 99.0}}
    assert rule.evaluate(state, small) is None


def test_alert_uses_joint_temperature_series():
    # joint_temperatures 의 특정 관절에서만 돌변 -> metric 이 해당 관절임
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
