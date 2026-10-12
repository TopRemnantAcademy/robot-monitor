"""告警抑制（inhibition）—— 对齐 Alertmanager `inhibit_rules`。

本模块提供**可追溯**的抑制判定与记录。核心语义（订单 30）：

    高等级告警抑制同一机器人上的低等级告警。

对比 `orders/10-dispatch.md` 落地的既有实现（`main.py` 的 offline 特例：
机器人离线时抑制同机全部衍生告警），本模块把「源」从**具体规则**泛化为
**该机当前活跃告警的最高 severity**，从而覆盖离线之外的组合：

    motor_temp(sev3) 活跃时  →  同机 battery_low(sev2) / joint_stuck(sev2) 被抑制

两条硬约束（订单 30 要求，写错就退化成「静默丢告警」）：

1. **不静默丢弃**：每一次抑制都必须留下可追溯证据 —— 结构化 `InhibitionRecord`
   + 日志（含 source 规则/severity 与 target 规则）+ 独立 Prometheus 指标。
   运维必须能回答「这条告警为什么没出去」，而不是只看到它凭空消失。
2. **同等级不互相抑制**：`severity` 严格大于才抑制。否则两颗 sev2 告警
   （battery_low 与 joint_stuck）会互相抑制，先进先出取决于字典序，是竞态。
3. **仅限同一机器人**（`equal: [robot_id]`）：A 机的高温不抑制 B 机的低电量。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.rules.base import Alert

log = logging.getLogger("inhibition")

#: 该机活跃告警的最高 severity ≥ 此值才作为抑制源。
#:
#: 取 3（critical）：只有 critical 才配压制同机的 warning/info。
#: 若取 2，则「电量低(2)」会压制「joint_stuck(2)」—— 同级互压属误伤；
#: 若取 1，则任何告警都会压制更低的，抑制面过宽，恢复后成片告警涌出。
MIN_SOURCE_SEVERITY = 3


@dataclass
class InhibitionRecord:
    """一次抑制的**可追溯**凭证（订单 30：禁止静默丢弃）。

    - `robot_id` / `target_rule` / `target_severity`：被压下去的那条告警
    - `source_rule` / `source_severity`：压制它的高等级告警（根因）
    - `reason`：机器可读成因（`offline` | `severity`），供指标与审计区分
    - `ts_ms`：抑制发生的毫秒时间戳
    """

    robot_id: str
    target_rule: str
    target_severity: int
    source_rule: str | None
    source_severity: int
    reason: str
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def as_log_fields(self) -> str:
        """扁平化为一行可 grep 的日志字段（脱敏：只含 rule_id / severity）。"""
        return (
            f"reason={self.reason} robot={self.robot_id} "
            f"target={self.target_rule}(sev{self.target_severity}) "
            f"source={self.source_rule}(sev{self.source_severity})"
        )


class ActiveAlertRegistry:
    """进程内「每台机器人当前活跃告警」登记簿。

    为什么需要它：severity 抑制的判据是「同机是否已有更高等级告警活跃」，
    而 `online_state` 只记在线布尔值，回答不了这个问题。
    消费线每次规则命中即登记，ticker 判恢复/离线即清理。

    为什么用进程内内存而非 Redis：与 `online_state`/`last_seen` 同源
    （main.py 已有先例）—— 活跃告警是**派生状态**，由本进程的规则评估直接产出，
    多一次 Redis 往返换不来正确性，却给热路径加 RTT。单副本假设与现有引擎一致。
    """

    def __init__(self) -> None:
        self._by_robot: dict[str, dict[str, Alert]] = {}

    def register(self, alert: Alert) -> None:
        """规则命中时登记/刷新该机该规则的最新告警。"""
        self._by_robot.setdefault(alert.robot_id, {})[alert.rule_id] = alert

    def clear(self, robot_id: str, rule_id: str) -> None:
        """规则恢复（本轮未再命中）时清除登记。"""
        rules = self._by_robot.get(robot_id)
        if not rules:
            return
        rules.pop(rule_id, None)
        if not rules:
            self._by_robot.pop(robot_id, None)

    def clear_robot(self, robot_id: str) -> None:
        """整机清除（如判离线时，衍生告警已无意义）。"""
        self._by_robot.pop(robot_id, None)

    def highest(self, robot_id: str, exclude_rule: str | None = None) -> Alert | None:
        """该机当前最高 severity 的活跃告警；`exclude_rule` 用于排除自身。"""
        rules = self._by_robot.get(robot_id)
        if not rules:
            return None
        candidates = [a for rid, a in rules.items() if rid != exclude_rule]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.severity)

    def snapshot(self) -> dict[str, dict[str, Alert]]:
        """只读快照（供测试与诊断断言，勿在热路径调用）。"""
        return {rid: dict(rules) for rid, rules in self._by_robot.items()}

    def clear_all(self) -> None:
        self._by_robot.clear()


def evaluate_severity_inhibition(
    alert: Alert,
    registry: ActiveAlertRegistry,
    min_source_severity: int = MIN_SOURCE_SEVERITY,
) -> InhibitionRecord | None:
    """纯函数：给定待发告警与活跃登记簿，判定是否被同机更高等级告警抑制。

    返回 `InhibitionRecord` 表示「被抑制」（附可追溯凭证），`None` 表示放行。
    **不产生副作用**（除日志），便于单测直接钉语义。
    """
    source = registry.highest(alert.robot_id, exclude_rule=alert.rule_id)
    if source is None:
        return None
    # 自身不得作为自己的抑制源（exclude_rule 已保证），且严格大于才抑制。
    if source.severity <= alert.severity:
        return None
    if source.severity < min_source_severity:
        return None
    return InhibitionRecord(
        robot_id=alert.robot_id,
        target_rule=alert.rule_id,
        target_severity=alert.severity,
        source_rule=source.rule_id,
        source_severity=source.severity,
        reason="severity",
    )
