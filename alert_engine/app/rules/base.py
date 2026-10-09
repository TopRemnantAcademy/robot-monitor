"""Rule 抽象基类（spec/02 §1）—— 四个规则类必须继承它。"""

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
    """所有规则的统一接口：`evaluate(state, window) -> Alert | None`。"""

    rule_id: str = "base"
    severity: int = 1
    dedup_window_s: int = 60

    @abstractmethod
    def evaluate(self, state: dict, window: deque) -> Alert | None:
        """state: 该机器人最新一条遥测（dict）；window: 最近若干条（deque）。"""
        raise NotImplementedError
