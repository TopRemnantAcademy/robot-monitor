"""Alert inhibition -- aligned with Alertmanager's `inhibit_rules`.

This module provides a **traceable** inhibition decision and record. Core semantics (order 30):

    A higher-severity alert inhibits lower-severity alerts on the same robot.

Compared with the existing implementation landed in `orders/10-dispatch.md` (the offline special case
in `main.py`: when a robot is offline, suppress all of its derivative alerts), this module generalizes
the "source" from a **concrete rule** to **the highest severity among that robot's currently active
alerts**, thereby covering combinations beyond offline:

    when motor_temp(sev3) is active -> this robot's battery_low(sev2) / joint_stuck(sev2) are inhibited

Two hard constraints (required by order 30; getting them wrong degrades into "silently dropping alerts"):

1. No silent dropping: every inhibition must leave traceable evidence -- a structured `InhibitionRecord`
   + logs (with source rule/severity and target rule) + an independent Prometheus metric.
   Operators must be able to answer "why didn't this alert go out", rather than just seeing it vanish.
2. Same severity does not inhibit each other: only a strict `severity` greater-than inhibits. Otherwise
   two sev2 alerts (battery_low and joint_stuck) would inhibit each other, with FIFO depending on dict
   order -- a race.
3. Same robot only (`equal: [robot_id]`): A's high temperature does not inhibit B's low battery.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.rules.base import Alert

log = logging.getLogger("inhibition")

#: The highest severity among this robot's active alerts must be >= this to act as an inhibition source.
#:
#: Set to 3 (critical): only critical is allowed to suppress this robot's warning/info.
#: If set to 2, then "low battery(2)" would suppress "joint_stuck(2)" -- same-level mutual suppression
#: is collateral damage; if set to 1, any alert would suppress a lower one, the inhibition surface is too
#: broad, and a flood of alerts would burst out on recovery.
MIN_SOURCE_SEVERITY = 3


@dataclass
class InhibitionRecord:
    """A **traceable** receipt for one inhibition (order 30: forbid silent dropping).

    - `robot_id` / `target_rule` / `target_severity`: the alert that got suppressed
    - `source_rule` / `source_severity`: the higher-severity alert that suppressed it (root cause)
    - `reason`: machine-readable cause (`offline` | `severity`), to distinguish metrics from audits
    - `ts_ms`: millisecond timestamp when the inhibition occurred
    """

    robot_id: str
    target_rule: str
    target_severity: int
    source_rule: str | None
    source_severity: int
    reason: str
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def as_log_fields(self) -> str:
        """Flatten into a single grep-able log line (desensitized: only rule_id / severity)."""
        return (
            f"reason={self.reason} robot={self.robot_id} "
            f"target={self.target_rule}(sev{self.target_severity}) "
            f"source={self.source_rule}(sev{self.source_severity})"
        )

    def as_detail_fields(self) -> dict:
        """Convert to forensic fields that can go into `robot_alert.detail` (JSONB) (order 35).

        Why the receipt goes into detail rather than a new column:
        the `suppressed_by` column only carries the scalar "who suppressed" for **query/filter** use;
        the severity comparison and reason are **forensic information** (read by humans, not used for
        filtering), and stuffing them into the existing JSONB column keeps the Schema surface minimal --
        AGENTS.md §4 requires Schema changes to sync init.sql with the ORM, one fewer column is one fewer
        drift surface.

        Key names are homomorphic with `as_log_fields()` (the `suppressed_*` prefix), convenient for
        log<->DB cross-reference.
        """
        return {
            "suppressed_reason": self.reason,
            "suppressed_source_rule": self.source_rule,
            "suppressed_source_severity": self.source_severity,
            "suppressed_target_severity": self.target_severity,
            "suppressed_ts_ms": self.ts_ms,
        }


class ActiveAlertRegistry:
    """In-process registry of "each robot's currently active alerts".

    Why it is needed: the criterion for severity inhibition is "does this robot already have a higher
    severity alert active", but `online_state` only records the online boolean and cannot answer that.
    The consume line registers on every rule hit; the ticker clears on recovery/offline.

    Why in-process memory rather than Redis: same source as `online_state`/`last_seen`
    (main.py already has a precedent) -- active alerts are **derived state**, produced directly by this
    process's rule evaluation; one more Redis round trip buys no correctness but adds RTT to the hot path.
    The single-replica assumption is consistent with the existing engine.
    """

    def __init__(self) -> None:
        self._by_robot: dict[str, dict[str, Alert]] = {}

    def register(self, alert: Alert) -> None:
        """Register/refresh the robot's latest alert for this rule when a rule hits."""
        self._by_robot.setdefault(alert.robot_id, {})[alert.rule_id] = alert

    def clear(self, robot_id: str, rule_id: str) -> None:
        """Clear the registration when the rule recovers (not hit this round)."""
        rules = self._by_robot.get(robot_id)
        if not rules:
            return
        rules.pop(rule_id, None)
        if not rules:
            self._by_robot.pop(robot_id, None)

    def clear_robot(self, robot_id: str) -> None:
        """Clear the whole robot (e.g. when judged offline, derivative alerts are meaningless)."""
        self._by_robot.pop(robot_id, None)

    def highest(self, robot_id: str, exclude_rule: str | None = None) -> Alert | None:
        """This robot's currently highest-severity active alert; `exclude_rule` excludes itself."""
        rules = self._by_robot.get(robot_id)
        if not rules:
            return None
        candidates = [a for rid, a in rules.items() if rid != exclude_rule]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.severity)

    def snapshot(self) -> dict[str, dict[str, Alert]]:
        """Read-only snapshot (for test and diagnostic assertions; do not call on the hot path)."""
        return {rid: dict(rules) for rid, rules in self._by_robot.items()}

    def clear_all(self) -> None:
        self._by_robot.clear()


def evaluate_severity_inhibition(
    alert: Alert,
    registry: ActiveAlertRegistry,
    min_source_severity: int = MIN_SOURCE_SEVERITY,
) -> InhibitionRecord | None:
    """Pure function: given the pending alert and the active registry, decide whether it is inhibited
    by a higher-severity alert on the same robot.

    Returns an `InhibitionRecord` meaning "inhibited" (with a traceable receipt), `None` meaning
    allow. **No side effects** (except logging), convenient for pinning semantics in unit tests.
    """
    source = registry.highest(alert.robot_id, exclude_rule=alert.rule_id)
    if source is None:
        return None
    # Itself must not be its own inhibition source (guaranteed by exclude_rule), and only a strict
    # greater-than inhibits.
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
