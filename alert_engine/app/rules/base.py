"""Rule abstract base class (spec/02 §1) — all four rule classes must inherit from it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Alert:
    robot_id: str
    rule_id: str
    severity: int                      # 1 info / 2 warning / 3 critical
    message: str
    detail: dict = field(default_factory=dict)


class Rule(ABC):
    """Unified interface for all rules: `evaluate(state, window) -> Alert | None`."""

    rule_id: str = "base"
    severity: int = 1
    dedup_window_s: int = 60

    @abstractmethod
    def evaluate(self, state: dict, window: deque) -> Alert | None:
        """state: the robot's latest telemetry record (dict); window: the most recent records (deque)."""
        raise NotImplementedError
