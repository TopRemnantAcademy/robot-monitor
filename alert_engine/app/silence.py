"""Alert silence -- aligned with Alertmanager `silence`.

Semantics: during maintenance/debugging, operators want a robot (or all of them) to **stop being noisy**.
Alerts hit within the silence window are neither persisted nor published; the TTL expires automatically,
so no manual clearing is required.

Redis key conventions (work order T3):
- `alert:silence:{robot_id}`  -- silence a single robot only
- `alert:silence:all`         -- silence everything (planned downtime, batch debugging)

Why Redis TTL rather than in-process state:
1. Silence is a "temporary state with a deadline", and TTL is its natural expression -- it disappears on
   expiry, so it cannot permanently suppress alerts because someone forgot to clear it (the classic mistake
   with an in-process boolean flag).
2. alert_engine may be deployed with multiple replicas, and silence must take effect across replicas,
   which in-process state cannot do.

fail-open (allow alerts through when Redis is unreachable):
Silence is a convenience layered on top of alerts, not part of data correctness.
Suppressing alerts while Redis is down would let a real fault go completely silent -- far more dangerous
than "silence fails and alerts get noisy". Same trade-off as `deduplicator.should_emit`'s fail-open.
"""

from __future__ import annotations

import logging

from app.db import get_redis
from app.rules.base import Alert

log = logging.getLogger("silence")

#: Global key for silencing everything.
SILENCE_ALL_KEY = "alert:silence:all"


def silence_key(robot_id: str) -> str:
    return f"alert:silence:{robot_id}"


async def set_silence(robot_id: str, ttl_s: int, all_robots: bool = False) -> None:
    """Write a silence (with TTL). When `all_robots=True` or robot_id is empty, write the global key.

    The interface is kept on the engine side (rather than only manually SET in Redis) so that
    "silence" and "unsilence" have explicit code entry points, convenient for future REST/CLI.
    """
    key = SILENCE_ALL_KEY if (all_robots or not robot_id) else silence_key(robot_id)
    r = await get_redis()
    await r.set(key, "1", ex=max(1, int(ttl_s)))
    log.info("silence set: %s ttl=%ds", key, ttl_s)


async def clear_silence(robot_id: str, all_robots: bool = False) -> None:
    """Clear a silence (before TTL expiry)."""
    key = SILENCE_ALL_KEY if (all_robots or not robot_id) else silence_key(robot_id)
    r = await get_redis()
    await r.delete(key)
    log.info("silence cleared: %s", key)


async def is_silenced(alert: Alert) -> bool:
    """Silence check. Hit either the global key or this robot's key to be considered silenced.

    The two GETs are merged into one MGET: the silence check runs on **every alert, every emit**,
    so in peak periods it is a hot path; two round-trip network RTTs would be pure waste.
    """
    try:
        r = await get_redis()
        vals = await r.mget(SILENCE_ALL_KEY, silence_key(alert.robot_id))
        return any(v is not None for v in vals)
    except Exception:
        # fail-open: when Redis is unreachable, allow the alert through; never suppress a fault alert
        # due to a silence-layer fault.
        log.exception("silence check failed, allowing emit (fail-open)")
        return False
