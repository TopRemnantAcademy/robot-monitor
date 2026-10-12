"""Redis 最新状态缓存 + Pub/Sub 广播（spec/01 §4.3）。

- Hash `robot:state:{robot_id}`，TTL 10s（断连后自动过期）
- 发布 `robot:stream:all`（全量）与 `robot:stream:{robot_id}`（单机）
- 同时写入预计算标量 max_joint_temp（spec/00 §4.3）
- 投递 `robot_metadata` 心跳（G-6 / ISSUE #96）：`put()` 只做一次入队，真正的
  UPDATE 由 metadata_writer 按（满 1s / 关闭）批量折叠后一条语句发出。

为什么心跳挂在 state_cache 而不是 mqtt_subscriber 里单独调
----------------------------------------------------------
`write_and_publish()` 是「这台机器人这一帧已被受理」的唯一事实点：
MQTT 收到 → pydantic 校验通过 → 每次合法遥测都会走到这里。
挂在消费循环里另起一处的话，以后再加数据源（HTTP 推流 / 回放）就会漏投心跳。
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
        # 在线台账（ISSUE #77）：与 Hash/PUBLISH 同一处、同一 try。
        # 它是「接收侧事实」（我们何时收到这一帧），与机器人自述的 status_code 不同源 ——
        # 停报的机器人不会再来这里，last_seen 自然不再前进。O(1) Redis 写，不碰数据库。
        await liveness.record(t.robot_id, t.status_code)

        # robot_metadata 心跳（G-6 / ISSUE #96）：入队即返回，不碰 DB。
        # 与 liveness.record 同在「这一帧已被受理」的时点，故两者语义一致。
        metadata_writer.put(t.robot_id, t.ts)

        payload = json.dumps(_decode(mapping), separators=(",", ":"))
        await r.publish(settings.chan_stream_all, payload)
        await r.publish(settings.chan_stream_one.format(robot_id=t.robot_id), payload)
    except Exception:
        log.exception("state_cache publish failed for %s", t.robot_id)


async def snapshot(robot_ids: list[str]) -> list[dict]:
    """拉取一批机器人的最新状态（WS 首帧快照 + `GET /robots`）。

    **必须枚举全部 robot_ids、不能只回 Redis 里存在的 Hash**（ISSUE #77 的连带问题）：
    Hash 带 10s TTL，机器人一停报就过期消失，只回存在的那些等于
    「掉线的机器人从大屏上凭空消失」——比误报在线更难排查。
    缺失的回落为离线占位（status_code=0 + is_online=false），
    与前端 `offlinePlaceholder()` 的首屏语义一致。

    `is_online` / `last_seen` 由 `liveness.enrich()` 批量补齐（2 个网络往返，非按台数）。
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
    """无遥测时的离线占位（后端侧与前端 `offlinePlaceholder` 字段逐一对齐）。

    与 `RobotTelemetryOut` 的必填字段一致：关节字典给空 dict 而非缺字段，
    否则前端 `Object.entries(joint_temperatures)` 会抛错。
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
    """把 Hash 里的字符串还原为前端期望的类型（spec/03 §7）。

    `is_online` / `last_seen` 不在这里给：它们不在遥测 Hash 里，而是
    「接收侧事实」，由 `liveness.enrich()` 从独立的台账补齐（ISSUE #77）。
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
