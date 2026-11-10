"""Motor over-temperature rule: any joint > 75°C sustained for >=3 consecutive samples (spec/02 §3)."""

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

        # Require EVERY one of the most recent min_samples samples to have a joint over threshold,
        # to avoid a single spike (noise) causing a false positive.
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
            message=(f"Motor over-temperature: {hot_joint} {hot_val:.1f}°C exceeds threshold "
                     f"{self.threshold}°C (consecutive {self.min_samples} samples)"),
            detail={"joint": hot_joint, "temp": hot_val, "threshold": self.threshold},
        )
