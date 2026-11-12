"""VLA policy-action MQTT subscription (R-VLA-EVAL Phase 1).

A *parallel* subscription to the telemetry one (`mqtt_subscriber.py`):

- Subscribe to `robots/+/action`
- Validate each payload against `PolicyAction` (the same contract the simulator
  and the external VLA node emit)
- Publish the latest action frame to the Redis channel `chan_action_all` (the WS
  hub reads from there and broadcasts `{"type":"action",...}`)
- Enqueue the validated frame onto the `PolicyActionBatchWriter` queue for
  TimescaleDB persistence into `policy_actions`

The action stream is fully decoupled from the telemetry stream: a broken action
payload is dropped + counted here, and never touches the 38w+ telemetry rows.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt
from pydantic import ValidationError

from app import metrics
from app.config import settings
from app.db.redis import get_redis
from app.schemas.telemetry import PolicyAction

log = logging.getLogger("action_subscriber")

TOPIC = "robots/+/action"

received = 0
dropped = 0


async def run(writer) -> None:
    delay = 1.0
    client = aiomqtt.Client(hostname=settings.mqtt_broker, port=settings.mqtt_port)
    while True:
        try:
            async with client:
                await client.subscribe(TOPIC)
                delay = 1.0
                log.info(
                    "action mqtt connected: %s:%d, subscribed %s",
                    settings.mqtt_broker, settings.mqtt_port, TOPIC,
                )
                async for message in client.messages:
                    await _handle(message.payload, writer)
        except aiomqtt.MqttError as exc:
            log.warning("action mqtt error (%s), reconnect in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def _handle(raw: bytes, writer) -> None:
    global dropped, received
    try:
        pa = PolicyAction.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        dropped += 1
        metrics.action_mqtt_dropped.inc()
        log.debug("invalid action payload dropped: %s", exc)
        return
    received += 1
    metrics.action_mqtt_received.inc()

    # Publish the latest action frame to the WS hub's Redis channel. The hub
    # coalesces by robot_id and broadcasts `{"type":"action",...}` on its beat.
    try:
        r = await get_redis()
        # model_dump preserves proper types (floats / list) -- the hub forwards
        # this verbatim as the `data` of the action frame.
        await r.publish(settings.chan_action_all, pa.model_dump_json())
    except Exception:
        log.exception("action redis publish failed for %s", pa.robot_id)

    await writer.put(pa)
