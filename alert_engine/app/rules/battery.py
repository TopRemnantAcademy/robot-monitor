"""低电量规则：battery_pct < 15%（spec/02 §3）。"""

from __future__ import annotations

from collections import deque

from app.config import settings
from app.rules.base import Alert, Rule


class BatteryLowRule(Rule):
    rule_id = "battery_low"
    severity = 2                                  # warning
    dedup_window_s = settings.battery_low_dedup_s

    def __init__(self) -> None:
        self.threshold = settings.battery_low_pct

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        pct = state.get("battery_pct")
        if pct is None:
            return None
        if pct >= self.threshold:
            return None
        return Alert(
            robot_id=state.get("robot_id", ""),
            rule_id=self.rule_id,
            severity=self.severity,
            message=f"低电量：{pct:.1f}% 低于阈值 {self.threshold}%",
            detail={"battery_pct": pct, "threshold": self.threshold},
        )
