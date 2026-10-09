"""Redis 最新状态缓存 + Pub/Sub 广播（spec/01 §4.3）。

- Hash `robot:state:{robot_id}`，TTL 10s（断连后自动过期）
- 发布 `robot:stream:all`（全量）与 `robot:stream:{robot_id}`（单机）
- 同时写入预计算标量 max_joint_temp（spec/00 §4.3）
"""

from __future__ import annotations

import json
import logging

from app.config import settings
from app.db.redis import get_redis
from app.schemas.telemetry import Telemetry

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
    """写入最新状态并广播（失败不得吞掉采集循环）。"""
    try:
        r = await get_redis()
        key = f"robot:state:{t.robot_id}"
        mapping = _state_hash(t)

        pipe = r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, settings.state_ttl_seconds)
        await pipe.execute()

        # 必须发布**还原类型后**的载荷：Redis Hash 只存字符串，
        # 直接广播会让前端收到 status_code="1"(字符串) 而非 1(数字)，
        # 违反 spec/03 §7 的 RobotTelemetry 类型，导致状态色与热力图判断全错。
        payload = json.dumps(_decode(mapping), separators=(",", ":"))
        await r.publish(settings.chan_stream_all, payload)
        await r.publish(settings.chan_stream_one.format(robot_id=t.robot_id), payload)
    except Exception:
        log.exception("state_cache publish failed for %s", t.robot_id)


async def snapshot(robot_ids: list[str]) -> list[dict]:
    """拉取一批机器人的最新状态（WS 首帧快照用）。"""
    r = await get_redis()
    out: list[dict] = []
    for rid in robot_ids:
        try:
            h = await r.hgetall(f"robot:state:{rid}")
        except Exception:
            continue
        if not h:
            continue
        out.append(_decode(h))
    return out


def _decode(h: dict[str, str]) -> dict:
    """把 Hash 里的字符串还原为前端期望的类型（spec/03 §7）。"""
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
