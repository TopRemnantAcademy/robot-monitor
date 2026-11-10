"""ISO 21423:2026-aligned robot status envelope and RobotMonitor mapping.

Context
-------
ISO 21423:2026 (ISO/TC 299, published 2026-10, announced by the Association
for Advancing Automation) defines common communication requirements for
industrial mobile robots (IMRs), fleet managers and connected systems. It builds
on VDA 5050 (MQTT + JSON) and the MassRobotics AMR interoperability standard and
standardises the exchange of:

  - location      (pose / node position)
  - speed / direction (velocity / twist)
  - operating condition (battery, mode, condition readings, errors)
  - tasking / availability

This module provides a lightweight, *ISO 21423-aligned* envelope
(``ISO21423RobotState``) modelled on those publicly documented common message
elements, plus lossless mappers between that envelope and RobotMonitor's
internal telemetry shape (a plain ``dict`` consumed by ``backend_ingestion``).

NOTE ON CONFORMANCE
-------------------
The normative field set lives behind the paywalled standard text. This envelope
implements the *common, publicly documented* elements and is structured so that
adding normative fields later is purely additive. It is NOT a claim of full
certification conformance.

Design invariants (mirrors the rest of RobotMonitor)
---------------------------------------------------
- pure / dependency-free: dataclasses only, no ROS or pydantic imports, so it
  is unit-testable with no network and no ``app``-package collision.
- lossless round-trip: ``telemetry_to_iso21423`` then ``iso21423_to_telemetry``
  reconstructs the original telemetry (asserted by the unit tests).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


class OperatingMode:
    """ISO 21423 operating-mode vocabulary (subset of the common elements).

    Higher severity numbers map to higher RobotMonitor status codes.
    """

    IDLE = "IDLE"
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"
    SERVICE = "SERVICE"
    ERROR = "ERROR"

    _SEVERITY = {IDLE: 1, AUTOMATIC: 1, MANUAL: 1, SERVICE: 2, ERROR: 3}

    @classmethod
    def normalize(cls, value: str | None) -> str:
        v = (value or "").upper()
        return v if v in cls._SEVERITY else cls.AUTOMATIC

    @classmethod
    def severity(cls, mode: str) -> int:
        return cls._SEVERITY.get(mode, 1)


@dataclass
class BatteryState:
    charge_pct: float = 0.0
    voltage_v: float = 0.0
    current_a: float = 0.0
    temperature_c: float = 0.0
    power_supply_status: int = 0


@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    qw: float = 1.0
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    map_id: str = ""


@dataclass
class Velocity:
    vx: float = 0.0
    vy: float = 0.0
    omega: float = 0.0


@dataclass
class ISO21423RobotState:
    """ISO 21423:2026-aligned robot status envelope (common elements)."""

    version: str
    manufacturer: str
    serial_number: str
    timestamp_ms: int
    operating_mode: str
    battery: BatteryState
    pose: Pose
    velocity: Velocity
    joint_positions: dict[str, float] = field(default_factory=dict)
    joint_torques: dict[str, float] = field(default_factory=dict)
    # Operating-condition readings (vendor-specific); joint temperatures belong
    # here. RobotMonitor treats this as ``joint_temperatures``.
    condition: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Mappers
# --------------------------------------------------------------------------

def yaw_to_quat(theta: float) -> tuple[float, float, float, float]:
    """Convert a planar yaw angle (radians) to a unit quaternion (w, x, y, z)."""
    half = theta / 2.0
    return (float(round(math.cos(half), 6)), 0.0, 0.0,
            float(round(math.sin(half), 6)))


def telemetry_to_iso21423(
    t: dict,
    *,
    manufacturer: str = "robotmonitor-sim",
    version: str = "1.0",
) -> ISO21423RobotState:
    """Wrap a RobotMonitor telemetry ``dict`` into an ISO 21423 envelope.

    Used by the simulator's ISO 21423 emit mode (and by any adapter that already
    holds RobotMonitor-shaped data) to prove the standard wire format.
    """
    sc = int(t.get("status_code", 1) or 1)
    ec = int(t.get("error_code", 0) or 0)
    if sc == 3 or ec != 0:
        mode = OperatingMode.ERROR
    elif sc == 2:
        mode = OperatingMode.SERVICE
    else:
        mode = OperatingMode.AUTOMATIC

    pose = Pose(
        x=float(t.get("pos_x") or 0.0),
        y=float(t.get("pos_y") or 0.0),
        z=float(t.get("pos_z") or 0.0),
        qw=float(t.get("orient_w") or 1.0),
        qx=float(t.get("orient_x") or 0.0),
        qy=float(t.get("orient_y") or 0.0),
        qz=float(t.get("orient_z") or 0.0),
        map_id="",
    )
    return ISO21423RobotState(
        version=version,
        manufacturer=manufacturer,
        serial_number=str(t.get("robot_id", "")),
        timestamp_ms=int(t.get("ts") or 0),
        operating_mode=mode,
        battery=BatteryState(
            charge_pct=float(t.get("battery_pct") or 0.0),
            voltage_v=0.0,
            current_a=0.0,
            temperature_c=float(t.get("battery_temp") or 0.0),
            power_supply_status=0,
        ),
        pose=pose,
        velocity=Velocity(vx=0.0, vy=0.0, omega=0.0),
        joint_positions=dict(t.get("joint_positions") or {}),
        joint_torques=dict(t.get("joint_torques") or {}),
        condition=dict(t.get("joint_temperatures") or {}),
        errors=[] if ec == 0 else [f"error_code={ec}"],
    )


def iso21423_to_telemetry(state: ISO21423RobotState) -> dict:
    """Convert an ISO 21423 envelope into a RobotMonitor telemetry ``dict``.

    The returned dict matches the schema validated by
    ``backend_ingestion/app/schemas/telemetry.py`` (modulo ``robot_id``; see the
    note on ``ROBOT_ID_PATTERN`` in ``docs/robot-integration.md``).
    """
    mode = state.operating_mode
    if mode == OperatingMode.ERROR or state.errors:
        status_code, error_code = 3, (1 if state.errors else 0)
    elif mode == OperatingMode.SERVICE:
        status_code, error_code = 2, 0
    else:
        status_code, error_code = 1, 0

    temps = dict(state.condition)
    max_temp = max(temps.values()) if temps else None
    p = state.pose

    return {
        "robot_id": state.serial_number,
        "status_code": status_code,
        "error_code": error_code,
        "battery_pct": round(state.battery.charge_pct, 2),
        "battery_temp": round(state.battery.temperature_c, 2)
        if state.battery.temperature_c else None,
        "max_joint_temp": round(max_temp, 2) if max_temp is not None else None,
        "joint_temperatures": {k: round(v, 2) for k, v in temps.items()},
        "joint_positions": {k: round(v, 4) for k, v in state.joint_positions.items()},
        "joint_torques": {k: round(v, 3) for k, v in state.joint_torques.items()},
        "pos_x": round(p.x, 3),
        "pos_y": round(p.y, 3),
        "pos_z": round(p.z, 3),
        "orient_w": round(p.qw, 4),
        "orient_x": round(p.qx, 4),
        "orient_y": round(p.qy, 4),
        "orient_z": round(p.qz, 4),
        "ts": state.timestamp_ms,
    }
