"""遥测 payload 校验 Schema（spec/01 §3）。

- robot_id 必须匹配 ^humanoid-[0-9]{3}$
- 三个关节字典键必须一致，且以 spec/00 §5.5 的 24 个标准键名为准
- max_joint_temp 缺失时由 joint_temperatures 推导（保证预计算列永远等于最高关节温度）
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# spec/00 §5.5 —— 唯一事实来源
JOINT_NAMES: tuple[str, ...] = (
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw", "left_elbow_pitch",
    "left_wrist_pitch", "left_wrist_roll",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw", "right_elbow_pitch",
    "right_wrist_pitch", "right_wrist_roll",
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch",
    "left_knee_pitch", "left_ankle_pitch", "left_ankle_roll",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch",
    "right_knee_pitch", "right_ankle_pitch", "right_ankle_roll",
)

ROBOT_ID_PATTERN = r"^humanoid-[0-9]{3}$"


class Telemetry(BaseModel):
    robot_id: str = Field(pattern=ROBOT_ID_PATTERN)
    status_code: int = 1                      # 0 离线 / 1 正常 / 2 预警 / 3 故障
    error_code: int = 0
    battery_pct: float = Field(ge=0.0, le=100.0)
    battery_temp: float | None = None
    max_joint_temp: float | None = None       # 预计算列，缺失时自动推导
    joint_temperatures: dict[str, float] = Field(min_length=1, max_length=40)
    joint_positions: dict[str, float] = Field(min_length=1, max_length=40)
    joint_torques: dict[str, float] = Field(min_length=1, max_length=40)
    pos_x: float | None = None
    pos_y: float | None = None
    pos_z: float | None = None
    orient_w: float | None = None
    orient_x: float | None = None
    orient_y: float | None = None
    orient_z: float | None = None
    ts: int | None = None                     # 客户端毫秒时间戳（可选）

    @field_validator("joint_temperatures", "joint_positions", "joint_torques")
    @classmethod
    def _same_keys(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("关节字典不可为空")
        return v

    @model_validator(mode="after")
    def _derive_max_joint_temp(self) -> "Telemetry":
        # 预计算列必须等于最高关节温度；未提供时由 temperatures 推导
        if self.max_joint_temp is None:
            self.max_joint_temp = max(self.joint_temperatures.values())
        return self
