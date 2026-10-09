"""MQTT 订阅（spec/01 §4.4）—— aiomqtt 异步订阅 + 掉线自动重连。

- 订阅 `robots/+/telemetry`
- 捕获 aiomqtt.MqttError 后指数退避重连（上限 30s）
- 收到消息 → Pydantic 校验 → 同时投递给 state_cache 与 batch_writer 队列
- 校验失败的消息**丢弃并计数**，不阻塞队列
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt
from pydantic import ValidationError

from app.config import settings
from app.schemas.telemetry import Telemetry
from app.services import state_cache

log = logging.getLogger("mqtt_subscriber")

TOPIC = "robots/+/telemetry"

dropped = 0
received = 0


async def run(writer) -> None:
    delay = 1.0
    while True:
        try:
            async with aiomqtt.Client(hostname=settings.mqtt_broker,
                                      port=settings.mqtt_port) as client:
                await client.subscribe(TOPIC)
                delay = 1.0
                log.info("mqtt connected: %s:%d, subscribed %s",
                         settings.mqtt_broker, settings.mqtt_port, TOPIC)
                async for message in client.messages:
                    await _handle(message.payload, writer)
        except aiomqtt.MqttError as exc:
            log.warning("mqtt error (%s), reconnect in %.1fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def _handle(raw: bytes, writer) -> None:
    global dropped, received
    try:
        t = Telemetry.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        dropped += 1
        log.debug("invalid payload dropped: %s", exc)
        return
    received += 1
    await state_cache.write_and_publish(t)   # 最新状态 + 广播
    await writer.put(t)                      # 批量落库队列
