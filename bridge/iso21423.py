"""ISO 21423:2026 envelope + RobotMonitor mappers.

Phase 0 Proof-of-Concept: the mapping logic is implemented in the compiled
Rust extension ``iso21423_rs`` (see ``bridge/iso21423-rs/``). This module keeps
the original pure-Python reference implementation in ``_iso21423_py`` and
re-exports the Rust-backed API by default, falling back to pure Python only if
the native extension cannot be imported. This guarantees the existing
``bridge/tests/`` suite and ``bridge/ros2_bridge.py`` keep working unchanged.
"""
try:
    from iso21423_rs import (  # type: ignore
        OperatingMode,
        BatteryState,
        Pose,
        Velocity,
        ISO21423RobotState,
        yaw_to_quat,
        telemetry_to_iso21423,
        iso21423_to_telemetry,
    )
    _BACKEND = "rust"
except ImportError:  # pragma: no cover - exercised only without the wheel
    from ._iso21423_py import (  # type: ignore
        OperatingMode,
        BatteryState,
        Pose,
        Velocity,
        ISO21423RobotState,
        yaw_to_quat,
        telemetry_to_iso21423,
        iso21423_to_telemetry,
    )
    _BACKEND = "python"

# `policy_action_to_wire` (R-VLA-EVAL Phase 1) is a newer addition. Tolerate a
# stale compiled extension: fall back to the pure-Python reference so the rest
# of the bridge keeps importing (the Rust port is re-exported once rebuilt).
try:
    from iso21423_rs import policy_action_to_wire  # type: ignore
except ImportError:  # pragma: no cover
    from ._iso21423_py import policy_action_to_wire  # type: ignore

__all__ = [
    "OperatingMode",
    "BatteryState",
    "Pose",
    "Velocity",
    "ISO21423RobotState",
    "yaw_to_quat",
    "telemetry_to_iso21423",
    "iso21423_to_telemetry",
    "policy_action_to_wire",
    "_BACKEND",
]
