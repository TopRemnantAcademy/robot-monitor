"""RobotMonitor real-robot bridge.

Bridges physical / simulated robots that speak ROS 2 (over rosbridge) or the
ISO 21423:2026 interoperability envelope into RobotMonitor's internal telemetry
ingest format.

Package layout
--------------
- ``iso21423``   : ISO 21423:2026-aligned envelope + lossless mappers.
- ``ros2_bridge``: rosbridge WebSocket client + pure ROS 2 -> envelope mapper.

The package is intentionally dependency-light (only ``websockets`` at runtime;
``aiomqtt`` is a lazy/optional import used only when relaying to the MQTT
ingest topic) and avoids importing the backend ``app`` package, so it can be
unit-tested in isolation without colliding with the two top-level ``app``
packages in the repo.

See ``docs/robot-integration.md`` for the architecture and mapping tables.
"""
