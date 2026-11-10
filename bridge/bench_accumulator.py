"""Micro-benchmark: compare the Rust vs pure-Python ROS2StateAccumulator.

Isolates the Phase 1 hot-path port. `iso21423_to_telemetry` is already Rust for
both backends (via the bridge.iso21423 shim), so any delta here is attributable
to the accumulator (ingest + build) being in Rust.

Run with:  bridge/.venv/Scripts/python.exe bridge/bench_accumulator.py
"""

from __future__ import annotations

import time

from bridge.ros2_bridge import (
    ROS2StateAccumulator,
    _ROS2StateAccumulatorPy,
    _ACC_BACKEND,
)
from bridge.iso21423 import iso21423_to_telemetry

# A realistic mix of rosbridge JSON messages (5 ROS 2 types).
_MSG_BATTERY = (
    "sensor_msgs/msg/BatteryState",
    {"percentage": 0.85, "voltage": 24.1, "current": -3.2,
     "temperature": 31.0, "power_supply_status": 1},
)
_MSG_JOINT = (
    "sensor_msgs/msg/JointState",
    {"name": ["base", "shoulder", "elbow"],
     "position": [0.1, 0.2, 0.3], "effort": [1.0, 2.0, 3.0]},
)
_MSG_ODOM = (
    "nav_msgs/msg/Odometry",
    {"pose": {"pose": {"position": {"x": 1.0, "y": 2.0, "z": 0.0},
                       "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}},
     "twist": {"twist": {"linear": {"x": 0.5, "y": 0.1},
                         "angular": {"z": 0.05}}}},
)
_MSG_POSE = (
    "geometry_msgs/msg/PoseStamped",
    {"pose": {"position": {"x": 1.0, "y": 2.0, "z": 0.0},
              "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}}},
)
_MSG_MODE = ("std_msgs/msg/String", {"data": "AUTOMATIC"})

_TEMPLATES = [_MSG_BATTERY, _MSG_JOINT, _MSG_ODOM, _MSG_POSE, _MSG_MODE]


def make_stream(n: int):
    return [_TEMPLATES[i % len(_TEMPLATES)] for i in range(n)]


def bench_acc_only(factory, label: str, stream) -> float:
    """ingest + build only (isolates the accumulator port)."""
    acc = factory()
    t0 = time.perf_counter()
    for mt, msg in stream:
        acc.ingest(mt, msg)
        acc.build(serial_number="R1", manufacturer="M", timestamp_ms=0)
    dt = time.perf_counter() - t0
    rate = len(stream) / dt
    print(f"  [{label}] accumulator only : {rate:,.0f} msg/s  ({dt:.3f}s)")
    return rate


def bench_pipeline(factory, label: str, stream) -> float:
    """ingest + build + iso21423_to_telemetry (realistic per-frame path)."""
    acc = factory()
    t0 = time.perf_counter()
    for mt, msg in stream:
        acc.ingest(mt, msg)
        env = acc.build(serial_number="R1", manufacturer="M", timestamp_ms=0)
        iso21423_to_telemetry(env)
    dt = time.perf_counter() - t0
    rate = len(stream) / dt
    print(f"  [{label}] full pipeline     : {rate:,.0f} msg/s  ({dt:.3f}s)")
    return rate


def main() -> None:
    N = 300_000
    print(f"backend in use (shim): {_ACC_BACKEND}")
    print(f"stream size: {N} messages (5 ROS 2 types cycled)\n")

    stream = make_stream(N)
    # warmup
    for mt, msg in make_stream(2000):
        ROS2StateAccumulator().ingest(mt, msg)

    print("Rust accumulator:")
    rust_acc = bench_acc_only(ROS2StateAccumulator, "rust", stream)
    rust_pipe = bench_pipeline(ROS2StateAccumulator, "rust", stream)

    print("Pure-Python accumulator:")
    py_acc = bench_acc_only(_ROS2StateAccumulatorPy, "python", stream)
    py_pipe = bench_pipeline(_ROS2StateAccumulatorPy, "python", stream)

    print("\nSpeedup (Rust / Python):")
    print(f"  accumulator only : {rust_acc / py_acc:.2f}x")
    print(f"  full pipeline    : {rust_pipe / py_pipe:.2f}x")


if __name__ == "__main__":
    main()
