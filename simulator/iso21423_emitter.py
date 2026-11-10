#!/usr/bin/env python3
"""ISO 21423:2026 emit mode for the RobotMonitor simulator.

Demonstrates that the simulator (and, by extension, any robot describable by a
RobotMonitor telemetry payload) can be wrapped in an ISO 21423:2026-aligned
envelope. For each generated payload the script:

  1. wraps it into an ``ISO21423RobotState`` -- the wire format a real robot /
     ROS 2 bridge would emit;
  2. losslessly converts it back to RobotMonitor telemetry via the bridge mapper;
  3. publishes that telemetry to the standard ingest topic
     ``robots/{id}/telemetry`` (so the existing ``backend_ingestion`` pipeline
     is unchanged);
  4. logs the envelope JSON to prove the standard format is in play.

This is the "simulator speaks the standard" half of the real-robot bridge. A
physical ROS 2 robot reaches the same ingest topic via ``bridge/ros2_bridge.py``
(rosbridge -> envelope -> telemetry -> MQTT). See ``docs/robot-integration.md``.

Run
---
    python simulator/iso21423_emitter.py --robots 5 --broker localhost --port 1883 --duration 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

# Make the repo root importable so `import bridge` and
# `from simulator.mock_fleet import ...` both resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.iso21423 import iso21423_to_telemetry, telemetry_to_iso21423  # noqa: E402
from simulator.mock_fleet import (  # noqa: E402
    JOINT_NAMES,
    PUBLISH_INTERVAL_S,
    RobotState,
    _next_ts_ms,
)

log = logging.getLogger("iso21423_emitter")

MANUFACTURER = "robotmonitor-sim"
TELEMETRY_TOPIC = "robots/{robot_id}/telemetry"


async def run_robot(state: RobotState, queue: asyncio.Queue, start_at: float) -> None:
    """Generate one robot's payloads at 10Hz and push ISO-21423-wrapped
    telemetry to the queue (reuses ``mock_fleet.RobotState`` waveforms)."""
    next_tick = time.monotonic()
    last_ts_ms: int | None = None
    while True:
        next_tick += PUBLISH_INTERVAL_S
        now = time.monotonic()
        await asyncio.sleep(max(0.0, next_tick - now))

        state.step(now, start_at)
        ts_ms = _next_ts_ms(last_ts_ms, int(time.time() * 1000))
        last_ts_ms = ts_ms

        payload = state.build_payload(ts_ms)
        # 1) wrap into the ISO 21423 envelope (the standard wire format)
        envelope = telemetry_to_iso21423(payload, manufacturer=MANUFACTURER)
        # 2) convert back to RobotMonitor telemetry (lossless mapper)
        telemetry = iso21423_to_telemetry(envelope)
        envelope_dict = envelope.to_dict()
        await queue.put((TELEMETRY_TOPIC.format(robot_id=state.robot_id),
                         json.dumps(telemetry, separators=(",", ":")),
                         envelope_dict))


async def publisher(queue: asyncio.Queue, broker: str, port: int) -> None:
    """Single MQTT writer: serial publish to the standard ingest topic."""
    import aiomqtt  # lazy: only when actually relaying to MQTT

    delay = 1.0
    while True:
        try:
            async with aiomqtt.Client(hostname=broker, port=port) as client:
                delay = 1.0
                while True:
                    topic, payload, envelope = await queue.get()
                    await client.publish(topic, payload, qos=0)
                    if envelope is not None:
                        log.info("ISO21423 envelope[%s]: %s",
                                 envelope["serial_number"], json.dumps(envelope))
        except aiomqtt.MqttError:
            log.warning("mqtt error; retry in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="[iso21423_emitter] %(message)s")
    states = [RobotState(robot_id=f"humanoid-{i:03d}")
              for i in range(1, args.robots + 1)]
    queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
    start_wall = time.monotonic()

    tasks = [asyncio.create_task(run_robot(s, queue, start_wall)) for s in states]
    tasks.append(asyncio.create_task(publisher(queue, args.broker, args.port)))
    try:
        if args.duration and args.duration > 0:
            await asyncio.sleep(args.duration)
        else:
            await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await t
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RobotMonitor ISO 21423 emit mode (simulator)")
    parser.add_argument("--robots", type=int, default=5)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Controlled seconds; >0 exits when reached (0=forever)")
    args = parser.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
