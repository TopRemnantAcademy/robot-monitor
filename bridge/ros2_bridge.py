"""ROS 2 -> RobotMonitor bridge over the rosbridge WebSocket protocol.

Two parts
--------
1. Pure mapper (``ROS2StateAccumulator``) -- converts ROS 2 messages (delivered
   by rosbridge as plain JSON) into an ``ISO21423RobotState`` envelope. Fully
   testable without ROS or a network.
2. ``RosbridgeClient`` -- connects to a rosbridge WebSocket server, subscribes to
   a configured set of topics, dispatches each incoming message to the mapper,
   and emits RobotMonitor telemetry ``dict`` s via an ``on_telemetry`` callback
   (or, optionally, publishes them to the RobotMonitor MQTT ingest topic via the
   lazy ``publish_telemetry`` sink).

Tested against a *fake rosbridge* WebSocket server (see
``bridge/tests/test_ros2_bridge.py``) so the transport path is exercised without
a real ROS 2 installation.

Why rosbridge
-------------
rosbridge_suite exposes any ROS 2 topic over a JSON WebSocket, which is exactly
the "JSON data interchange over a publish/subscribe bus" model ISO 21423:2026
standardises on. A physical robot running ROS 2 + rosbridge therefore becomes a
first-class RobotMonitor source with no proprietary agent.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Awaitable, Callable

from bridge.iso21423 import (
    ISO21423RobotState,
    OperatingMode,
    Pose,
    Velocity,
    iso21423_to_telemetry,
)

log = logging.getLogger("ros2_bridge")

# topic -> ROS 2 message type (rosbridge needs the type to decode)
DEFAULT_ROS_TOPICS: dict[str, str] = {
    "/battery_state": "sensor_msgs/msg/BatteryState",
    "/joint_states": "sensor_msgs/msg/JointState",
    "/odom": "nav_msgs/msg/Odometry",
}


# --------------------------------------------------------------------------
# Pure ROS 2 message -> envelope accumulator
# --------------------------------------------------------------------------

class ROS2StateAccumulator:
    """Accumulates ROS 2 messages (as plain JSON) for one robot into an
    ISO 21423 envelope. Pure (no I/O) -- fully unit-testable.
    """

    def __init__(self) -> None:
        self._battery = BatteryLike(0.0)
        self._pose = None
        self._velocity = None
        self._joint_positions: dict[str, float] = {}
        self._joint_torques: dict[str, float] = {}
        self._condition: dict[str, float] = {}
        self._mode = OperatingMode.AUTOMATIC
        self._errors: list[str] = []

    # -- ingest dispatch --------------------------------------------------
    def ingest(self, msg_type: str, msg: dict) -> None:
        handler = _ROS_HANDLERS.get(msg_type)
        if handler is None:
            log.debug("ros2_bridge: no handler for msg_type %s", msg_type)
            return
        handler(self, msg)

    def set_mode(self, mode: str) -> None:
        self._mode = OperatingMode.normalize(mode)

    def add_error(self, err: str) -> None:
        if err and err not in self._errors:
            self._errors.append(err)

    # -- envelope construction -------------------------------------------
    def build(
        self,
        *,
        serial_number: str,
        manufacturer: str,
        version: str = "1.0",
        timestamp_ms: int | None = None,
    ) -> ISO21423RobotState:
        return ISO21423RobotState(
            version=version,
            manufacturer=manufacturer,
            serial_number=serial_number,
            timestamp_ms=timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
            operating_mode=self._mode,
            battery=self._battery,
            pose=self._pose or Pose(x=0.0, y=0.0, z=0.0, qw=1.0,
                                    qx=0.0, qy=0.0, qz=0.0, map_id=""),
            velocity=self._velocity or Velocity(vx=0.0, vy=0.0, omega=0.0),
            joint_positions=self._joint_positions,
            joint_torques=self._joint_torques,
            condition=self._condition,
            errors=list(self._errors),
        )


class BatteryLike:
    """Minimal battery container shared by the accumulator and the mapper."""

    def __init__(self, charge_pct: float, voltage_v: float = 0.0,
                 current_a: float = 0.0, temperature_c: float = 0.0,
                 power_supply_status: int = 0) -> None:
        self.charge_pct = charge_pct
        self.voltage_v = voltage_v
        self.current_a = current_a
        self.temperature_c = temperature_c
        self.power_supply_status = power_supply_status


# -- per-message-type handlers ----------------------------------------------

def _h_battery(acc: ROS2StateAccumulator, msg: dict) -> None:
    raw = msg.get("percentage")
    if raw is None:
        pct = 0.0
    else:
        f = float(raw)
        # ROS convention is 0.0..1.0; some fleets publish 0..100. Treat >1 as
        # already a percentage to avoid 90x inflation.
        pct = f * 100.0 if f <= 1.0 else f
    acc._battery = BatteryLike(
        charge_pct=pct,
        voltage_v=float(msg.get("voltage", 0.0)),
        current_a=float(msg.get("current", 0.0)),
        temperature_c=float(msg.get("temperature", 0.0)),
        power_supply_status=int(msg.get("power_supply_status", 0)),
    )


def _h_joint_state(acc: ROS2StateAccumulator, msg: dict) -> None:
    names = msg.get("name") or []
    positions = msg.get("position") or []
    efforts = msg.get("effort") or []
    for i, name in enumerate(names):
        if i < len(positions):
            acc._joint_positions[name] = float(positions[i])
        if i < len(efforts):
            acc._joint_torques[name] = float(efforts[i])
    # JointState carries no temperature; a vendor thermal topic would populate
    # acc._condition (RobotMonitor's joint_temperatures). Left as-is here.


def _h_odometry(acc: ROS2StateAccumulator, msg: dict) -> None:
    pose_obj = (msg.get("pose") or {}).get("pose") or {}
    pos = pose_obj.get("position") or {}
    ori = pose_obj.get("orientation") or {}
    acc._pose = Pose(
        x=float(pos.get("x", 0.0)),
        y=float(pos.get("y", 0.0)),
        z=float(pos.get("z", 0.0)),
        qw=float(ori.get("w", 1.0)),
        qx=float(ori.get("x", 0.0)),
        qy=float(ori.get("y", 0.0)),
        qz=float(ori.get("z", 0.0)),
        map_id="",
    )
    twist = (msg.get("twist") or {}).get("twist") or {}
    lin = twist.get("linear") or {}
    ang = twist.get("angular") or {}
    acc._velocity = Velocity(
        vx=float(lin.get("x", 0.0)),
        vy=float(lin.get("y", 0.0)),
        omega=float(ang.get("z", 0.0)),
    )


def _h_pose_stamped(acc: ROS2StateAccumulator, msg: dict) -> None:
    pose_obj = msg.get("pose") or {}
    pos = pose_obj.get("position") or {}
    ori = pose_obj.get("orientation") or {}
    acc._pose = Pose(
        x=float(pos.get("x", 0.0)),
        y=float(pos.get("y", 0.0)),
        z=float(pos.get("z", 0.0)),
        qw=float(ori.get("w", 1.0)),
        qx=float(ori.get("x", 0.0)),
        qy=float(ori.get("y", 0.0)),
        qz=float(ori.get("z", 0.0)),
        map_id="",
    )


def _h_string_mode(acc: ROS2StateAccumulator, msg: dict) -> None:
    data = msg.get("data")
    if data:
        acc.set_mode(str(data))


_ROS_HANDLERS = {
    "sensor_msgs/msg/BatteryState": _h_battery,
    "sensor_msgs/msg/JointState": _h_joint_state,
    "nav_msgs/msg/Odometry": _h_odometry,
    "geometry_msgs/msg/PoseStamped": _h_pose_stamped,
    "geometry_msgs/msg/PoseWithCovarianceStamped": _h_pose_stamped,
    "std_msgs/msg/String": _h_string_mode,
}


# --------------------------------------------------------------------------
# rosbridge WebSocket client
# --------------------------------------------------------------------------

class RosbridgeClient:
    """Connects to a rosbridge server, subscribes to topics, and emits
    RobotMonitor telemetry dicts for each incoming ROS 2 message.
    """

    def __init__(
        self,
        *,
        manufacturer: str,
        serial_number: str,
        topics: dict[str, str] | None = None,
        on_telemetry: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self.manufacturer = manufacturer
        self.serial_number = serial_number
        self.topics = topics or DEFAULT_ROS_TOPICS
        self.on_telemetry = on_telemetry
        self._acc = ROS2StateAccumulator()

    async def _subscribe(self, ws) -> None:
        for topic, msg_type in self.topics.items():
            await ws.send(json.dumps(
                {"op": "subscribe", "topic": topic, "type": msg_type,
                 "throttle_rate": 0}))

    async def _handle(self, raw: str) -> dict | None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if frame.get("op") != "publish":
            return None
        msg_type = frame.get("type") or self.topics.get(frame.get("topic", ""))
        msg = frame.get("msg")
        if not msg_type or not isinstance(msg, dict):
            return None
        self._acc.ingest(msg_type, msg)
        envelope = self._acc.build(
            serial_number=self.serial_number, manufacturer=self.manufacturer)
        return iso21423_to_telemetry(envelope)

    async def run(self, url: str) -> None:
        import websockets  # lazy: only needed when actually connecting

        async with websockets.connect(url) as ws:
            await self._subscribe(ws)
            async for raw in ws:
                telemetry = await self._handle(raw)
                if telemetry is not None and self.on_telemetry is not None:
                    await self.on_telemetry(telemetry)


# --------------------------------------------------------------------------
# Optional MQTT sink (lazy import of aiomqtt)
# --------------------------------------------------------------------------

async def publish_telemetry(mqtt_client, telemetry: dict) -> None:
    """Publish a RobotMonitor telemetry dict to the standard ingest topic.

    ``mqtt_client`` is an ``aiomqtt.Client`` (lazy-imported by callers). The
    topic matches what ``mock_fleet`` / the ISO 21423 emitter publish, so the
    existing ``backend_ingestion`` pipeline ingests real-robot data unchanged.
    """
    robot_id = telemetry.get("robot_id", "unknown")
    await mqtt_client.publish(
        f"robots/{robot_id}/telemetry",
        json.dumps(telemetry, separators=(",", ":")),
        qos=0,
    )


async def _amain() -> None:
    """CLI entry: bridge a ROS 2 robot (via rosbridge) into RobotMonitor MQTT."""
    import os

    import aiomqtt  # optional dependency; only imported when run as a relay

    url = os.environ.get("ROSBRIDGE_URL", "ws://localhost:9090")
    manufacturer = os.environ.get("ROBOT_MANUFACTURER", "unknown")
    serial = os.environ.get("ROBOT_SERIAL", "robot-001")
    broker = os.environ.get("MQTT_BROKER", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))

    async with aiomqtt.Client(hostname=broker, port=port) as mqtt:
        async def sink(telemetry: dict) -> None:
            await publish_telemetry(mqtt, telemetry)

        client = RosbridgeClient(
            manufacturer=manufacturer, serial_number=serial, on_telemetry=sink)
        log.info("ros2_bridge: relaying %s -> mqtt %s:%s", url, broker, port)
        await client.run(url)


def main() -> None:
    import asyncio
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
