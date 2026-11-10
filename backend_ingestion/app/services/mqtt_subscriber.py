"""MQTT subscription (spec/01 SS4.4) -- aiomqtt async subscription + auto
reconnect on disconnect.

- Subscribe to `robots/+/telemetry`
- On aiomqtt.MqttError, exponential backoff reconnect (cap 30s)
- On message -> Pydantic validation -> deliver to both state_cache and the
  batch_writer queue
- Messages that fail validation are **dropped and counted**, not queue-blocking
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt
from pydantic import ValidationError

from app import metrics
from app.config import settings
from app.schemas.telemetry import Telemetry
from app.services import state_cache

log = logging.getLogger("mqtt_subscriber")

TOPIC = "robots/+/telemetry"

dropped = 0
received = 0


async def run(writer) -> None:
    delay = 1.0
    # Client is constructed outside the loop and reused inside -- per the official
    # docs:
    # "The `Client` context is designed to be reusable (but not reentrant)"
    # (external/aiomqtt/docs/reconnection.md @ v2.5.1).
    client = aiomqtt.Client(hostname=settings.mqtt_broker,
                            port=settings.mqtt_port)
    while True:
        try:
            async with client:
                await client.subscribe(TOPIC)
                delay = 1.0
                log.info("mqtt connected: %s:%d, subscribed %s",
                         settings.mqtt_broker, settings.mqtt_port, TOPIC)
                async for message in client.messages:
                    await _handle(message.payload, writer)
        except aiomqtt.MqttError as exc:
            # Exponential backoff 1->30s (not the official example's fixed 5s):
            # under 100 units x 10Hz = 1000 msg/s, fixed high-frequency reconnect
            # during broker jitter would worsen congestion.
            # Why aiomqtt>=2.5.0 (verified via source + runtime, see
            # external/ORDER12-VERIFICATION.md SS2): 2.5.0 removed the redundant
            # re-raise of the disconnect exception in `__aexit__`, leaving the
            # MessagesIterator as the single MqttError throw point; 2.0.1 would
            # re-throw once more on `async with` exit after `async for` already
            # threw MqttError (double throw / masks the real stack). Both paths
            # reach this except for reconnection.
            # Ref CHANGELOG 2.5.0: "Let exceptions bubble up in Client.__aexit__ ..."
            log.warning("mqtt error (%s), reconnect in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def _handle(raw: bytes, writer) -> None:
    global dropped, received
    try:
        t = Telemetry.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        dropped += 1
        # Export to Prometheus: the in-process `dropped` counter is invisible;
        # only here does `robot_backend_mqtt_dropped_total` actually move (>0 =
        # payload contract broken).
        metrics.mqtt_dropped.inc()
        log.debug("invalid payload dropped: %s", exc)
        return
    received += 1
    # ISSUE #72: the old `robot_monitor_mqtt_messages_total` and
    # `robot_backend_mqtt_received_total` were co-sourced (same `received` count);
    # the duplicate count has been removed -- only one counter remains here.
    metrics.mqtt_received.inc()
    await state_cache.write_and_publish(t)   # latest state + broadcast
    await writer.put(t)                      # batch flush queue
