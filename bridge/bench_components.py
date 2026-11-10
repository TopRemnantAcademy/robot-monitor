"""Component-level breakdown of the per-frame ROS 2 -> telemetry path.

Decomposes RosbridgeClient._handle into the stages that the Rust port did (or
could) touch, so we can see exactly where the CPU time goes and whether Phase 2
(porting _handle's JSON decode/dispatch to Rust) is worth it.

Stages
------
1. json.loads(raw_frame)            -- WebSocket frame decode  (Phase 2 target)
2. acc.ingest(msg_type, msg)       -- ROS 2 -> accumulator     (Phase 1: Rust)
3. acc.build(...)                  -- accumulator -> envelope  (Phase 1: Rust)
4. iso21423_to_telemetry(env)      -- envelope -> telemetry    (already Rust)

Run:  bridge/.venv/Scripts/python.exe -m bridge.bench_components
"""

from __future__ import annotations

import json
import time

from bridge.iso21423 import iso21423_to_telemetry
from bridge.ros2_bridge import (
    ROS2StateAccumulator,
    _ACC_BACKEND,
    _ROS2StateAccumulatorPy,
)

MSG_TYPE = "nav_msgs/msg/Odometry"
MSG = {
    "pose": {
        "pose": {
            "position": {"x": 1.0, "y": 2.0, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        }
    },
    "twist": {
        "twist": {
            "linear": {"x": 0.5, "y": 0.1},
            "angular": {"z": 0.05},
        }
    },
}
RAW_FRAME = json.dumps(
    {"op": "publish", "topic": "/odom", "type": MSG_TYPE, "msg": MSG}
)


def time_it(fn, n: int, label: str) -> float:
    for _ in range(2000):  # warmup
        fn()
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    dt = time.perf_counter() - t0
    ns = dt / n * 1e9
    print(f"  {label:34s}: {ns:8.1f} ns/op   ({n / dt:,.0f} op/s)")
    return ns


def main() -> None:
    print(f"backend in use (shim): {_ACC_BACKEND}")
    print(f"(deployed path uses the Rust accumulator)\n")

    print("== Deployed path: Rust accumulator ==")
    acc = ROS2StateAccumulator()
    t_decode = time_it(lambda: json.loads(RAW_FRAME), 200_000, "1. json.loads(raw frame)")
    t_ingest = time_it(lambda: acc.ingest(MSG_TYPE, MSG), 200_000, "2. acc.ingest (parse)")
    t_build = time_it(
        lambda: acc.build(serial_number="R1", manufacturer="M", timestamp_ms=0),
        200_000, "3. acc.build (envelope)")
    env = acc.build(serial_number="R1", manufacturer="M", timestamp_ms=0)
    t_map = time_it(lambda: iso21423_to_telemetry(env), 200_000, "4. iso21423_to_telemetry")
    total = t_decode + t_ingest + t_build + t_map
    print(f"\n  reconstructed full-path : {total/1e6:.2f} ms/op  ->  {1e9/total:,.0f} msg/s")
    print("  share: decode {:.1f}% | ingest {:.1f}% | build {:.1f}% | map {:.1f}%".format(
        100 * t_decode / total, 100 * t_ingest / total,
        100 * t_build / total, 100 * t_map / total))

    print("\n== Same stages with the pure-Python accumulator ==")
    acc_py = _ROS2StateAccumulatorPy()
    t_ingest_py = time_it(lambda: acc_py.ingest(MSG_TYPE, MSG), 200_000, "2. acc.ingest (python)")
    t_build_py = time_it(
        lambda: acc_py.build(serial_number="R1", manufacturer="M", timestamp_ms=0),
        200_000, "3. acc.build (python)")
    env_py = acc_py.build(serial_number="R1", manufacturer="M", timestamp_ms=0)
    t_map_py = time_it(lambda: iso21423_to_telemetry(env_py), 200_000, "4. iso21423_to_telemetry (python env)")

    print("\n== Phase 2 ROI read ==")
    print(f"  ingest : Rust {t_ingest:7.1f} ns vs Python {t_ingest_py:7.1f} ns  "
          f"({t_ingest_py/t_ingest:.2f}x faster in Rust)")
    print(f"  build  : Rust {t_build:7.1f} ns vs Python {t_build_py:7.1f} ns  "
          f"({t_build_py/t_build:.2f}x faster in Rust)")
    print(f"  decode : {t_decode:7.1f} ns  (NOT yet ported; this is what Phase 2 would touch)")
    print(f"  map    : {t_map:7.1f} ns  (already Rust for both; dominant cost)")


if __name__ == "__main__":
    main()
