"""Unit tests for the ISO 21423 envelope and RobotMonitor mappers.

Pure / no network: exercises the dataclasses and the two mappers directly.
"""
import math
import os
import sys

# Make the repo root importable regardless of pytest import mode.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge.iso21423 import (  # noqa: E402
    ISO21423RobotState,
    OperatingMode,
    Pose,
    Velocity,
    BatteryState,
    iso21423_to_telemetry,
    telemetry_to_iso21423,
    yaw_to_quat,
)


def _sample_telemetry() -> dict:
    """A realistic RobotMonitor payload already at the mapper's rounding precision."""
    return {
        "robot_id": "humanoid-001",
        "status_code": 1,
        "error_code": 0,
        "battery_pct": 82.5,
        "battery_temp": 31.2,
        "max_joint_temp": 58.34,
        "joint_temperatures": {
            "left_shoulder_pitch": 58.34,
            "right_elbow_pitch": 41.10,
        },
        "joint_positions": {"left_shoulder_pitch": 0.1234, "right_elbow_pitch": -0.5},
        "joint_torques": {"left_shoulder_pitch": 3.210, "right_elbow_pitch": 1.0},
        "pos_x": 12.345,
        "pos_y": -3.210,
        "pos_z": 0.0,
        "orient_w": 0.9999,
        "orient_x": 0.0,
        "orient_y": 0.0,
        "orient_z": 0.0141,
        "ts": 1700000000123,
    }


def test_telemetry_to_envelope_preserves_fields():
    t = _sample_telemetry()
    env = telemetry_to_iso21423(t, manufacturer="acme")
    assert env.serial_number == "humanoid-001"
    assert env.manufacturer == "acme"
    assert env.operating_mode == OperatingMode.AUTOMATIC
    assert env.battery.charge_pct == 82.5
    assert env.battery.temperature_c == 31.2
    assert env.condition["left_shoulder_pitch"] == 58.34
    assert env.joint_positions["left_shoulder_pitch"] == 0.1234
    assert env.pose.x == 12.345 and env.pose.z == 0.0
    assert env.timestamp_ms == 1700000000123


def test_envelope_to_telemetry_status_derivation():
    base = _sample_telemetry()

    ok = telemetry_to_iso21423(base)
    back = iso21423_to_telemetry(ok)
    assert back["status_code"] == 1 and back["error_code"] == 0

    fault = dict(base, status_code=3, error_code=1)
    back_f = iso21423_to_telemetry(telemetry_to_iso21423(fault))
    assert back_f["status_code"] == 3 and back_f["error_code"] == 1

    warn = dict(base, status_code=2)
    back_w = iso21423_to_telemetry(telemetry_to_iso21423(warn))
    assert back_w["status_code"] == 2


def test_round_trip_is_lossless_for_precision_aligned_input():
    t = _sample_telemetry()
    env = telemetry_to_iso21423(t)
    back = iso21423_to_telemetry(env)
    # Fields that survive the round-trip exactly (input pre-rounded).
    assert back["robot_id"] == t["robot_id"]
    assert back["status_code"] == t["status_code"]
    assert back["battery_pct"] == t["battery_pct"]
    assert back["max_joint_temp"] == t["max_joint_temp"]
    assert back["joint_temperatures"] == t["joint_temperatures"]
    assert back["joint_positions"] == t["joint_positions"]
    assert back["joint_torques"] == t["joint_torques"]
    assert back["pos_x"] == t["pos_x"] and back["pos_y"] == t["pos_y"]
    assert back["orient_w"] == t["orient_w"] and back["orient_z"] == t["orient_z"]


def test_error_list_produces_fault_status():
    env = ISO21423RobotState(
        version="1.0", manufacturer="acme", serial_number="R-9", timestamp_ms=1,
        operating_mode=OperatingMode.AUTOMATIC,
        battery=BatteryState(charge_pct=50.0),
        pose=Pose(), velocity=Velocity(), errors=["e-stop pressed"],
    )
    back = iso21423_to_telemetry(env)
    assert back["status_code"] == 3
    assert back["error_code"] == 1


def test_empty_condition_yields_null_max_temp():
    env = ISO21423RobotState(
        version="1.0", manufacturer="acme", serial_number="R-1", timestamp_ms=1,
        operating_mode=OperatingMode.IDLE, battery=BatteryState(charge_pct=10.0),
        pose=Pose(), velocity=Velocity(), condition={},
    )
    back = iso21423_to_telemetry(env)
    assert back["max_joint_temp"] is None
    assert back["joint_temperatures"] == {}


def test_yaw_to_quat_is_unit_norm():
    for theta in (0.0, math.pi / 2, math.pi, -1.234):
        w, x, y, z = yaw_to_quat(theta)
        norm = math.sqrt(w * w + x * x + y * y + z * z)
        assert abs(norm - 1.0) < 1e-6
