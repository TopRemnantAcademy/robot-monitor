"""告警落库 + 分发（spec/02 §5）。

- 写入 TimescaleDB `robot_alert` 超表
- **PUBLISH `robot:alerts`** —— 前端告警流的唯一数据源，缺此则前端永远空白
"""

from __future__ import annotations

import json
import logging
import time

from app.config import settings
from app.db import get_pool, get_redis
from app.rules.base import Alert

log = logging.getLogger("store")

INSERT_SQL = """
INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def save(alert: Alert) -> None:
    from datetime import datetime, timezone

    pool = await get_pool()
    try:
        # 走 acquire() 而不是 pool.execute()：前者失败时会**丢弃**这条连接，
        # 后者在连接处于异常状态（如网络中断）时可能被放回池中复用，导致后续写入连环失败。
        # 告警是低基数事件（去重后 60~120s 一条/机器人），这里多一层 acquire 无性能代价。
        async with pool.acquire() as conn:
            await conn.execute(
                INSERT_SQL,
                datetime.now(timezone.utc),
                alert.robot_id,
                alert.rule_id,
                alert.severity,
                alert.message,
                json.dumps(alert.detail),
            )
    except Exception:
        log.exception("alert insert failed: %s/%s", alert.robot_id, alert.rule_id)

    try:
        r = await get_redis()
        await r.publish(settings.chan_alerts, json.dumps({
            "robot_id": alert.robot_id,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "message": alert.message,
            "detail": alert.detail,
            "ts": int(time.time() * 1000),
        }, separators=(",", ":")))
    except Exception:
        log.exception("alert publish failed: %s/%s", alert.robot_id, alert.rule_id)


async def update_online(robot_id: str, online: bool) -> None:
    """维护 robot_metadata.is_online / last_seen（仅在状态变化时调用）。"""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE robot_metadata SET is_online=$2, last_seen=NOW() WHERE robot_id=$1",
                robot_id, online,
            )
    except Exception:
        log.exception("metadata update failed: %s", robot_id)
