"""告警去重器（spec/02 §4）—— 必须幂等，禁止重复写库。

Redis Key `alert:dedup:{robot_id}:{rule_id}`，SET 时带 EX = 去重窗口秒数。
同一 (robot_id, rule_id) 在窗口内只产生一条告警；窗口过期后允许下一次触发
（恢复后再次告警）。
"""

from __future__ import annotations

import logging

from app.db import get_redis
from app.rules.base import Alert

log = logging.getLogger("deduplicator")


async def should_emit(alert: Alert, window_s: int) -> bool:
    """返回 True 表示允许发告警（首次触发或窗口已过期）。"""
    try:
        r = await get_redis()
        key = f"alert:dedup:{alert.robot_id}:{alert.rule_id}"
        # SET key 1 EX window NX —— NX 保证只有一个发起者能通过（幂等）
        ok = await r.set(key, "1", ex=max(1, int(window_s)), nx=True)
        return bool(ok)
    except Exception:
        log.exception("dedup check failed, allowing emit (fail-open)")
        return True
