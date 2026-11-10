"""Redis latest-state cache + Pub/Sub broadcast (spec/01 SS4.3).

- Hash `robot:state:{robot_id}`, TTL 10s (auto-expire after disconnect)
- Publish `robot:stream:all` (all) and `robot:stream:{robot_id}` (single unit)
- Also write the precomputed scalar max_joint_temp (spec/00 SS4.3)
- Deliver `robot_metadata` heartbeat (G-6 / ISSUE #96): `put()` only enqueues
  once; the real UPDATE is folded by metadata_writer per (1s full / closed) into
  a single statement sent later.

Why the heartbeat hangs on state_cache rather than being called separately in
mqtt_subscriber
-----------------------------------------------------------------------
`write_and_publish()` is the single fact point of "this robot's frame has been
accepted":
MQTT received -> pydantic validation passed -> every legal telemetry reaches
here.
If a separate call were added in the consume loop, adding a data source later
(HTTP push / replay) would miss the heartbeat.
"""

from __future__ import annotations

import json
import logging

from app.config import settings
from app.db.redis import get_redis
from app.schemas.telemetry import Telemetry
from app.services import liveness
from app.services.metadata_writer import metadata_writer

log = logging.getLogger("state_cache")


def _state_hash(t: Telemetry) -> dict[str, str]:
    return {
        "robot_id": t.robot_id,
        "status_code": str(t.status_code),
        "error_code": str(t.error_code),
        "battery_pct": str(t.battery_pct),
        "max_joint_temp": str(t.max_joint_temp),
        "joint_temperatures": json.dumps(t.joint_temperatures),
        "joint_positions": json.dumps(t.joint_positions),
        "joint_torques": json.dumps(t.joint_torques),
        "pos_x": str(t.pos_x or 0.0),
        "pos_y": str(t.pos_y or 0.0),
        "pos_z": str(t.pos_z or 0.0),
        "ts": str(t.ts or 0),
    }


async def write_and_publish(t: Telemetry) -> None:
    """Write latest state and broadcast (failure must not swallow the ingest loop)."""
    try:
        r = await get_redis()
        key = f"robot:state:{t.robot_id}"
        mapping = _state_hash(t)

        pipe = r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, settings.state_ttl_seconds)
        await pipe.execute()

        # Must publish the **type-restored** payload: the Redis Hash stores only
        # strings; broadcasting directly would make the frontend receive
        # status_code="1" (string) instead of 1 (number), violating spec/03 SS7's
        # RobotTelemetry type and breaking status-color and heatmap judgments.
        # Online ledger (ISSUE #77): same place and same try as Hash/PUBLISH.
        # It is "receive-side fact" (when we received this frame), co-sourced with
        # the robot's self-reported status_code -- a robot that stops reporting
        # will not come here again, so last_seen naturally stops advancing. O(1)
        # Redis write, no DB touch.
        await liveness.record(t.robot_id, t.status_code)

        # robot_metadata heartbeat (G-6 / ISSUE #96): enqueue and return, no DB.
        # Same moment as liveness.record -- "this frame accepted" -- so the two are
        # semantically consistent.
        metadata_writer.put(t.robot_id, t.ts)

        payload = json.dumps(_decode(mapping), separators=(",", ":"))
        await r.publish(settings.chan_stream_all, payload)
        await r.publish(settings.chan_stream_one.format(robot_id=t.robot_id), payload)
    except Exception:
        log.exception("state_cache publish failed for %s", t.robot_id)


async def snapshot(robot_ids: list[str]) -> list[dict]:
    """Fetch the latest state of a batch of robots (WS first-frame snapshot +
    `GET /robots`).

    **Must enumerate all robot_ids, not only the Hash existing in Redis**
    (a collateral issue of ISSUE #77): the Hash carries a 10s TTL; once a robot
    stops reporting it expires and disappears, so returning only the existing ones
    equals "the offline robot vanishes from the big-screen" -- harder to debug
    than falsely reporting online. Missing ones fall back to an offline
    placeholder (status_code=0 + is_online=false), matching the frontend
    `offlinePlaceholder()` first-screen semantics.

    `is_online` / `last_seen` are batch-filled by `liveness.enrich()` (2 network
    round-trips, not per-unit).
    """
    r = await get_redis()
    out: list[dict] = []
    for rid in robot_ids:
        try:
            h = await r.hgetall(f"robot:state:{rid}")
        except Exception:
            continue
        out.append(_decode(h) if h else offline_placeholder(rid))
    return await liveness.enrich(out)


def offline_placeholder(robot_id: str) -> dict:
    """Offline placeholder when there is no telemetry (backend-side, field-by-field
    aligned with the frontend `offlinePlaceholder`).

    Matches `RobotTelemetryOut`'s required fields: joint dicts are empty dicts
    rather than absent fields, otherwise the frontend
    `Object.entries(joint_temperatures)` would throw.
    """
    return {
        "robot_id": robot_id,
        "status_code": 0,
        "error_code": 0,
        "battery_pct": 0.0,
        "max_joint_temp": 0.0,
        "joint_temperatures": {},
        "joint_positions": {},
        "joint_torques": {},
        "ts": 0,
    }


def _decode(h: dict[str, str]) -> dict:
    """Restore the Hash's strings to the types the frontend expects (spec/03 SS7).

    `is_online` / `last_seen` are not produced here: they are not in the telemetry
    Hash, but are "receive-side fact", batch-filled by `liveness.enrich()` from a
    separate ledger (ISSUE #77).
    """
    import json as _json

    def num(v: str | None, default=0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    return {
        "robot_id": h.get("robot_id", ""),
        "status_code": int(num(h.get("status_code"), 0)),
        "error_code": int(num(h.get("error_code"), 0)),
        "battery_pct": num(h.get("battery_pct")),
        "max_joint_temp": num(h.get("max_joint_temp")),
        "joint_temperatures": _json.loads(h.get("joint_temperatures") or "{}"),
        "joint_positions": _json.loads(h.get("joint_positions") or "{}"),
        "joint_torques": _json.loads(h.get("joint_torques") or "{}"),
        "ts": int(num(h.get("ts"), 0)),
    }
