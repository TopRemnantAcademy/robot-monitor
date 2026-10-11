"""告警落库 + 分发（spec/02 §5）。

- 写入 TimescaleDB `robot_alert` 超表
- **PUBLISH `robot:alerts`** —— 前端告警流的唯一数据源，缺此则前端永远空白

两段是**独立** try/except（刻意的：发不出去不该拦住落库，落库失败也不该拦住前端告警流）。
代价是二者会各自静默失败，所以 insert 失败必须同时递增
`robot_alert_store_errors_total`，否则「DB 里没有」与「根本没告警」无法区分。
"""

from __future__ import annotations

import json
import logging
import time

from app import metrics
from app.config import settings
from app.db import get_pool, get_redis
from app.rules.base import Alert

log = logging.getLogger("store")

# RETURNING alert_id：WS 告警信封必须带上 alert_id，前端的「确认」按钮
# 才能寻址到具体一条（订单 17）。此前不返回 id，前端拿不到寻址键。
INSERT_SQL = """
INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
RETURNING alert_id
"""


async def save(alert: Alert) -> None:
    from datetime import datetime, timezone

    pool = await get_pool()
    try:
        # 走 acquire() 而不是 pool.execute()：前者失败时会**丢弃**这条连接，
        # 后者在连接处于异常状态（如网络中断）时可能被放回池中复用，导致后续写入连环失败。
        # 告警是低基数事件（去重后 60~120s 一条/机器人），这里多一层 acquire 无性能代价。
        async with pool.acquire() as conn:
            alert_id = await conn.fetchval(
                INSERT_SQL,
                datetime.now(timezone.utc),
                alert.robot_id,
                alert.rule_id,
                alert.severity,
                alert.message,
                json.dumps(alert.detail),
            )
    except Exception:
        # 指标 + 日志，缺一不可（G-4）：
        # - 日志留现场（堆栈、robot_id），供值班定位到具体是哪条连接/哪条告警；
        #   只打日志的行，**外部无人知晓**——看板的告警流由下面的 PUBLISH 驱动，
        #   落库失败但发布成功时，前端该告警照常滚动，看板绿得理直气壮。
        # - Counter 让 Prometheus 能用 increase(...[5m]) 判「正在失败」，
        #   并配上 robot-alerts.yml 的 AlertStoreFailing 规则 → 真正的告警的告警。
        metrics.store_errors.inc()
        log.exception("alert insert failed: %s/%s", alert.robot_id, alert.rule_id)
        alert_id = None

    try:
        r = await get_redis()
        # 信封结构不变（禁止项 3）：仍是扁平 JSON，仍是 type:"alert" 的 data。
        # 新增两个**数据字段**（不新增信封类型）：
        #   alert_id —— 前端 ack/unack 的寻址键；
        #   escalated —— 初发恒为 false，前端据此统一渲染字段（避免 undefined 分支）。
        await r.publish(settings.chan_alerts, json.dumps({
            "robot_id": alert.robot_id,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "message": alert.message,
            "detail": alert.detail,
            "ts": int(time.time() * 1000),
            "alert_id": alert_id,
            "escalated": False,
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
