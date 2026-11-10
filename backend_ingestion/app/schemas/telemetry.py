"""Telemetry payload validation schema (spec/01 §3).

- robot_id must match ^humanoid-[0-9]{3}$
- the three joint dicts must share keys, based on the 24 standard key names from spec/00 §5.5
- when max_joint_temp is absent it is derived from joint_temperatures (ensuring the
  precomputed column always equals the highest joint temperature)
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# spec/00 §5.5 — single source of truth
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
    status_code: int = 1                      # 0=offline / 1=ok / 2=warning / 3=fault
    error_code: int = 0
    battery_pct: float = Field(ge=0.0, le=100.0)
    battery_temp: float | None = None
    max_joint_temp: float | None = None       # precomputed column; auto-derived when absent
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
    ts: int | None = None                     # client millisecond timestamp (optional)

    @field_validator("joint_temperatures", "joint_positions", "joint_torques")
    @classmethod
    def _same_keys(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("joint dict must not be empty")
        return v

    @model_validator(mode="after")
    def _derive_max_joint_temp(self) -> "Telemetry":
        # precomputed column must equal the highest joint temperature; when absent
        # it is derived from temperatures
        if self.max_joint_temp is None:
            self.max_joint_temp = max(self.joint_temperatures.values())
        return self
