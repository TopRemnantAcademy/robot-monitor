"""电机过热规则：任一关节 > 75°C 且**持续 ≥3 个采样点**（spec/02 §3）。"""

from __future__ import annotations

from collections import deque

from app.config import settings
from app.rules.base import Alert, Rule


class MotorTempRule(Rule):
    rule_id = "motor_temp"
    severity = 3                                  # critical
    dedup_window_s = settings.motor_temp_dedup_s

    def __init__(self) -> None:
        self.threshold = settings.motor_temp_c
        self.min_samples = settings.motor_temp_min_samples

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        if len(window) < self.min_samples:
            return None

        # 要求最近 min_samples 个采样点**每一个**都有关节超阈值，
        # 避免单个尖峰（噪声）误报。
        hot_joint, hot_val = None, None
        for w in list(window)[-self.min_samples:]:
            temps = w.get("joint_temperatures") or {}
            if not temps:
                return None
            j, v = max(temps.items(), key=lambda kv: kv[1])
            if v <= self.threshold:
                return None
            hot_joint, hot_val = j, v

        return Alert(
            robot_id=state.get("robot_id", ""),
            rule_id=self.rule_id,
            severity=self.severity,
            message=(f"电机过热：{hot_joint} {hot_val:.1f}°C 超过阈值 {self.threshold}°C"
                     f"（连续 {self.min_samples} 个采样点）"),
            detail={"joint": hot_joint, "temp": hot_val, "threshold": self.threshold},
        )
