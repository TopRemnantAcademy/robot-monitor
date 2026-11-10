"""Joint-stuck rule: a joint whose angular variance is ~0 but whose torque stays over threshold (spec/02 §3).

Detection: within a window (default 5s ~= 50 samples), for some joint
  - joint_positions variance < 1e-6 (almost no movement), AND
  - joint_torques mean > 8.0 (still applying force)
=> classified as stuck (command is moving, but the joint is not).

Joint names are taken from the KEYS of the latest telemetry record; we do not hard-code the
24 joint names here, to avoid drift versus the three definitions in spec/00 §5.5 / backend / simulator.
"""

from __future__ import annotations

from collections import deque
from statistics import mean, pvariance

from app.config import settings
from app.rules.base import Alert, Rule

SAMPLE_HZ = 10                                    # 10Hz


class JointStuckRule(Rule):
    rule_id = "joint_stuck"
    severity = 2                                  # warning
    dedup_window_s = settings.joint_stuck_dedup_s

    def __init__(self) -> None:
        self.window_s = settings.joint_stuck_window_s
        self.var_threshold = settings.joint_stuck_variance
        self.torque_threshold = settings.joint_stuck_torque
        self.need_samples = max(2, int(self.window_s * SAMPLE_HZ))

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        if len(window) < self.need_samples:
            return None
        recent = list(window)[-self.need_samples:]

        joints = (state.get("joint_positions") or {}).keys()
        for j in joints:
            angles, torques = [], []
            ok = True
            for w in recent:
                a = (w.get("joint_positions") or {}).get(j)
                t = (w.get("joint_torques") or {}).get(j)
                if a is None or t is None:
                    ok = False
                    break
                angles.append(a)
                torques.append(t)
            if not ok:
                continue

            var = pvariance(angles)
            tq = mean(torques)
            if var < self.var_threshold and tq > self.torque_threshold:
                return Alert(
                    robot_id=state.get("robot_id", ""),
                    rule_id=self.rule_id,
                    severity=self.severity,
                    message=(f"Joint stuck: {j} angle variance {var:.2e} nearly stationary, "
                             f"but mean torque {tq:.1f} exceeds threshold {self.torque_threshold}"),
                    detail={"joint": j, "variance": var, "mean_torque": tq},
                )
        return None
