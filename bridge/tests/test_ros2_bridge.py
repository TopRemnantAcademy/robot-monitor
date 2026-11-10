"""Unit tests for the ROS 2 -> RobotMonitor bridge.

Covers the pure ``ROS2StateAccumulator`` mapper directly, and the
``RosbridgeClient`` transport end-to-end against a *fake rosbridge* WebSocket
server (no real ROS 2 needed).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest  # noqa: E402
import websockets  # noqa: E402

from bridge.ros2_bridge import (  # noqa: E402
    DEFAULT_ROS_TOPICS,
    ROS2StateAccumulator,
    RosbridgeClient,
)

# Sample ROS 2 messages (as rosbridge would deliver them: plain JSON).
_SAMPLE = {
    "/battery_state": {
        "op": "publish", "topic": "/battery_state",
        "type": "sensor_msgs/msg/BatteryState",
        "msg": {"percentage": 0.82, "voltage": 24.1, "current": -1.5,
                "temperature": 30.0, "power_supply_status": 2},
    },
    "/joint_states": {
        "op": "publish", "topic": "/joint_states",
        "type": "sensor_msgs/msg/JointState",
        "msg": {"name": ["wheel_left", "wheel_right"], "position": [0.1, 0.2],
                "velocity": [0.5, 0.5], "effort": [1.2, 1.3]},
    },
    "/odom": {
        "op": "publish", "topic": "/odom",
        "type": "nav_msgs/msg/Odometry",
        "msg": {
            "pose": {"pose": {"position": {"x": 1.5, "y": 2.5, "z": 0.0},
                              "orientation": {"w": 0.999, "x": 0.0,
                                              "y": 0.0, "z": 0.044}}},
            "twist": {"twist": {"linear": {"x": 0.3, "y": 0.0, "z": 0.0},
                                "angular": {"x": 0.0, "y": 0.0, "z": 0.05}}},
        },
    },
}


async def _fake_rosbridge_handler(ws):
    async for raw in ws:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if frame.get("op") == "subscribe":
            topic = frame.get("topic")
            if topic in _SAMPLE:
                await ws.send(json.dumps(_SAMPLE[topic]))


def test_accumulator_ingest_battery_and_odom():
    acc = ROS2StateAccumulator()
    acc.ingest("sensor_msgs/msg/BatteryState",
               {"percentage": 0.5, "voltage": 12.0, "temperature": 25.0})
    acc.ingest("nav_msgs/msg/Odometry", {
        "pose": {"pose": {"position": {"x": 3.0, "y": 4.0},
                          "orientation": {"w": 1.0}}},
        "twist": {"twist": {"linear": {"x": 1.0}, "angular": {"z": 0.2}}},
    })
    env = acc.build(serial_number="X-1", manufacturer="mfr")
    assert env.battery.charge_pct == 50.0
    assert env.battery.voltage_v == 12.0
    assert env.pose.x == 3.0 and env.pose.y == 4.0
    assert env.velocity.vx == 1.0 and env.velocity.omega == 0.2


def test_accumulator_ingest_joint_state():
    acc = ROS2StateAccumulator()
    acc.ingest("sensor_msgs/msg/JointState",
               {"name": ["a", "b"], "position": [0.1, 0.2], "effort": [2.0, 3.0]})
    env = acc.build(serial_number="X-2", manufacturer="mfr")
    assert env.joint_positions == {"a": 0.1, "b": 0.2}
    assert env.joint_torques == {"a": 2.0, "b": 3.0}


def test_accumulator_ingest_unknown_type_is_ignored():
    acc = ROS2StateAccumulator()
    acc.ingest("some_msgs/msg/Unknown", {"foo": 1})
    env = acc.build(serial_number="X-3", manufacturer="mfr")
    # No crash; falls back to defaults.
    assert env.battery.charge_pct == 0.0
    assert env.pose.x == 0.0


def test_ros2_client_end_to_end_with_fake_bridge():
    asyncio.run(_e2e())


async def _e2e() -> None:
    collected: list[dict] = []

    async def on_telemetry(t: dict) -> None:
        collected.append(t)

    server = await websockets.serve(_fake_rosbridge_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = RosbridgeClient(
        manufacturer="acme", serial_number="R-7", on_telemetry=on_telemetry)
    task = asyncio.create_task(client.run(f"ws://127.0.0.1:{port}"))
    try:
        for _ in range(150):
            if len(collected) >= len(DEFAULT_ROS_TOPICS):
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()

    assert collected, "no telemetry received from fake rosbridge"
    last = collected[-1]
    assert last["robot_id"] == "R-7"
    # ROS fraction 0.82 -> 82.0%
    assert last["battery_pct"] == pytest.approx(82.0)
    assert last["joint_torques"]["wheel_left"] == pytest.approx(1.2)
    assert last["pos_x"] == pytest.approx(1.5)
    assert last["pos_y"] == pytest.approx(2.5)
    assert last["orient_z"] == pytest.approx(0.044)
