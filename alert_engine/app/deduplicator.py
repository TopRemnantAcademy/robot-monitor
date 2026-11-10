"""Alert deduplicator (spec/02 §4) — must be idempotent; duplicate writes to the database are forbidden.

Redis key `alert:dedup:{robot_id}:{rule_id}`, SET with EX = dedup window in seconds.
The same (robot_id, rule_id) produces only one alert within the window; after the window expires the
next trigger is allowed (re-alert after recovery).
"""

from __future__ import annotations

import logging

from app.db import get_redis
from app.rules.base import Alert

log = logging.getLogger("deduplicator")


async def should_emit(alert: Alert, window_s: int) -> bool:
    """Return True to allow emitting the alert (first trigger, or the window has expired)."""
    try:
        r = await get_redis()
        key = f"alert:dedup:{alert.robot_id}:{alert.rule_id}"
        # SET key 1 EX window NX -- NX guarantees only one initiator can pass (idempotent)
        ok = await r.set(key, "1", ex=max(1, int(window_s)), nx=True)
        return bool(ok)
    except Exception:
        log.exception("dedup check failed, allowing emit (fail-open)")
        return True
