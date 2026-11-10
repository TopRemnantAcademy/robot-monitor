"""Alert escalation -- aligned with Alertmanager's `repeat_interval`.

Problem: after a severity=3 alert is sent, if the on-duty operator **does not see / does not ack** it,
it stays silent in the alert stream forever -- "alert sent but nobody acts" is the most expensive
failure mode of a monitoring system.

Semantics (work order T2): severity=3 and past `escalate_after_s` (default 300s) without `ack`
-> set `escalated=TRUE` and **re-**`PUBLISH robot:alerts` (envelope still `type:"alert"`,
forbidden item 3). The frontend shows "escalated" accordingly.

Why "scan DB" instead of "in-memory timer":
- An in-memory timer is lost entirely after a process restart, which is equivalent to "cancel all
  escalations" on restart -- a monitoring system's escalation capability must not depend on process
  survival.
- The alert is **already persisted** (store.save); the DB is its authoritative state. The `ack` /
  `escalated` columns are durable facts; scanning them yields a judgment independent of restart, and
  is naturally consistent across replicas.

Repeat count: `alert:escalated:{alert_id}` (Redis counter, with TTL).
The DB's `escalated` is only the boolean "has been escalated", while "how many times escalated" uses
the Redis counter: we do not recommend adding another column just for the count (the spirit of order 17
"forbid adding redundant columns" -- only add columns that the business truly requires).
The counter TTL is set to several times the escalation threshold; expiry does not affect semantics
(at most allowing extra re-sends).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.config import settings
from app.db import get_pool, get_redis
from app import metrics

log = logging.getLogger("escalation")

#: Scan SQL.
#:
#: The WHERE predicate must match the partial index `idx_alert_escalation` in init.sql:206
#: verbatim (`ack = FALSE AND escalated = FALSE AND severity = 3`); otherwise the index is not hit,
#: and each scan degrades into a sequential scan of robot_alert.
#:
#: The time window uses `time <= NOW() - escalate_after_s` to express "timed out".
#: `ORDER BY time` processes the earliest timed-out alert first (fairness), and combined with LIMIT
#: prevents any single round from running too long.
SCAN_SQL = """
SELECT alert_id, time, robot_id, rule_id, severity, message, detail
  FROM robot_alert
 WHERE ack = FALSE
   AND escalated = FALSE
   AND severity = 3
   AND time <= NOW() - make_interval(secs => $1)
 ORDER BY time
 LIMIT $2
"""

MARK_SQL = """
UPDATE robot_alert
   SET escalated = TRUE
 WHERE alert_id = $1 AND ack = FALSE AND escalated = FALSE
RETURNING alert_id
"""

#: Max items processed per round: prevents a backlog from occupying the event loop in one scan
#: (N UPDATEs + N PUBLISHes per round).
SCAN_BATCH = 200


def _repeat_key(alert_id: int) -> str:
    return f"alert:escalated:{alert_id}"


async def escalate_once() -> int:
    """Scan one round and escalate; return the number of alerts escalated this round. Pure-function style: easy to drive a single round directly in tests."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(SCAN_SQL, settings.escalate_after_s, SCAN_BATCH)
        if not rows:
            return 0
        escalated = await _escalate_rows(conn, rows)
    return escalated


async def _escalate_rows(conn, rows) -> int:
    """Escalate row by row on a single connection (ensures scan and UPDATE share the same connection semantics)."""
    r = await get_redis()
    escalated = 0
    for row in rows:
        alert_id = int(row["alert_id"])
        try:
            # Use a Redis counter as a guardrail for "re-send at most N times". INCR is atomic,
            # so even when multiple replicas scan the same row concurrently, only one replica gets "1".
            n = await r.incr(_repeat_key(alert_id))
            if n == 1:
                # First count: set a TTL longer than the threshold, to avoid unbounded key accumulation.
                await r.expire(
                    _repeat_key(alert_id),
                    max(60, int(settings.escalate_after_s) * (settings.escalate_max_repeat + 2)),
                )
            if n > settings.escalate_max_repeat:
                # Reached the re-send cap: only count, do not re-send, to avoid "nobody acts -> forever spamming".
                log.warning(
                    "escalation capped: %s %s alert_id=%s repeats=%d",
                    row["robot_id"], row["rule_id"], alert_id, n,
                )
                continue

            # Conditional UPDATE: `ack = FALSE AND escalated = FALSE` ensures that under concurrency
            # only one replica can flip this row to TRUE (other replicas get rowcount=0 -> skip re-send).
            marked = await conn.fetchrow(MARK_SQL, alert_id)
            if marked is None:
                continue

            await _publish_escalated(row)
            metrics.alerts_escalated.labels(rule_id=row["rule_id"]).inc()
            escalated += 1
            log.warning(
                "alert ESCALATED: %s %s sev=%d alert_id=%s (unacknowledged over %ds, repeat=%d)",
                row["robot_id"], row["rule_id"], row["severity"], alert_id,
                settings.escalate_after_s, n,
            )
        except Exception:
            log.exception("escalate failed: alert_id=%s", alert_id)
    return escalated


async def _publish_escalated(row) -> None:
    """Re-send to `robot:alerts`.

    The envelope **must still be type:"alert"** (forbidden item 3): the frontend `useRobotWebSocket`
    branches on `type` in three ways; changing the type would drop the escalated alert into the default
    branch and discard it. Use the `escalated: true` **data field** to express "this is an escalated
    re-send", rather than adding a new envelope type.
    """
    detail = row["detail"]
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    if not isinstance(detail, dict):
        detail = {}

    payload = {
        "robot_id": row["robot_id"],
        "rule_id": row["rule_id"],
        "severity": row["severity"],
        "message": row["message"],
        "detail": detail,
        "ts": int(time.time() * 1000),
        # The frontend shows "escalated" from this; alert_id is also kept for the frontend "ack" button to address.
        "escalated": True,
        "alert_id": int(row["alert_id"]),
    }
    r = await get_redis()
    await r.publish(settings.chan_alerts, json.dumps(payload, separators=(",", ":")))


async def escalation_loop() -> None:
    """Periodic scan. Runs concurrently with consume / ticker (see main.amain)."""
    log.info(
        "escalation loop started: after=%ds interval=%.1fs max_repeat=%d",
        settings.escalate_after_s, settings.escalate_scan_interval_s,
        settings.escalate_max_repeat,
    )
    while True:
        await asyncio.sleep(settings.escalate_scan_interval_s)
        try:
            await escalate_once()
        except Exception:
            # A single round's failure must not exit the whole escalation loop: after DB hiccups
            # recover, the next round continues.
            log.exception("escalation scan failed")
