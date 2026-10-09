"""关节卡死规则：同一关节角度方差≈0 但扭矩持续超阈值（spec/02 §3）。

判定：窗口（默认 5s ≈ 50 个采样点）内某关节
  - joint_positions 方差 < 1e-6（几乎不动），且
  - joint_torques 均值 > 8.0（仍在发力）
→ 判定为卡死（命令在动、实际没动）。

关节名取自**最新一条遥测的键**，不在此处重复硬编码 24 个名字，
避免与 spec/00 §5.5 / 后端 / 模拟器三处定义漂移。
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
                    message=(f"关节卡死：{j} 角度方差 {var:.2e} 近乎不动，"
                             f"但平均扭矩 {tq:.1f} 超阈值 {self.torque_threshold}"),
                    detail={"joint": j, "variance": var, "mean_torque": tq},
                )
        return None
