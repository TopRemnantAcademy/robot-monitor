"""Alert persistence + distribution (spec/02 §5).

- Write to TimescaleDB `robot_alert` hypertable
- **PUBLISH `robot:alerts`** -- the sole data source for the frontend alert stream; without it the
  frontend is forever blank

The two stages are **independent** try/except (deliberate: a failed send should not block persistence,
and a persistence failure should not block the frontend alert stream).
The cost is that each can fail silently, so an insert failure MUST also increment
`robot_alert_store_errors_total`, otherwise "not in DB" and "no alert at all" become indistinguishable.
"""

from __future__ import annotations

import json
import logging
import time

from app import metrics
from app.config import settings
from app.db import get_pool, get_redis
from app.inhibition import InhibitionRecord
from app.rules.base import Alert

log = logging.getLogger("store")

# RETURNING alert_id: the WS alert envelope must carry alert_id so the frontend "ack" button
# can address a specific row (order 17). Previously id was not returned, so the frontend could not get the addressing key.
INSERT_SQL = """
INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
RETURNING alert_id
"""

# Persistence statement for suppressed alerts (order 35).
#
# [Why a separate SQL is required, and cannot reuse INSERT_SQL]
# The suppression record does NOT go through `save()`: `save()`'s second half PUBLISHes `robot:alerts`,
# and "not pushing to the frontend" is exactly the semantics of suppression -- reusing it would degrade
# suppression into a pass-through.
#
# [Why it must be persisted: order 30's "no silent dropping" is only truly closed here]
# Order 30 already produced `InhibitionRecord` (with source/target/reason), and did WARNING logging
# and the `robot_alert_suppressed_total` metric. But **once an alert is suppressed it is not written to
# the DB**, so `GET /alerts` simply has no such row -- operators can only grep logs and count metrics,
# unable to answer in the alert console "what else did this machine report at the time, and who suppressed it".
# This statement turns the receipt into a `robot_alert` row (suppressed=TRUE), making suppression
# queryable, auditable, and presentable.
#
# The traceable receipt is stuffed into detail (JSONB, no new column): `suppressed_by` is only "who
# suppressed", while the severity comparison and cause (offline / severity) are forensic information,
# best kept in detail to minimize Schema surface.
# The merge happens on the Python side (`save_suppressed`'s `dict(alert.detail)` + `update(...)`) --
# NOT `detail || $6::jsonb` in SQL: `alert.detail`'s old value is what was **already stored** in that
# column, whereas here we want to merge the detail produced by the rule **this time**; the two are not
# the same (the latter is the authoritative input).
SUPPRESSED_INSERT_SQL = """
INSERT INTO robot_alert
    (time, robot_id, rule_id, severity, message, detail, suppressed, suppressed_by)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, TRUE, $7)
RETURNING alert_id
"""


async def save(alert: Alert) -> None:
    from datetime import datetime, timezone

    pool = await get_pool()
    try:
        # Use acquire() instead of pool.execute(): the former DISCARDS the connection on failure,
        # while the latter may return a connection in an abnormal state (e.g. network interruption) to
        # the pool for reuse, causing subsequent writes to fail in a chain.
        # Alerts are low-cardinality events (one per robot every 60~120s after dedup), so this extra
        # acquire layer costs no performance here.
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
        # Metric + log, neither can be missing (G-4):
        # - Logs preserve the scene (stack, robot_id) for on-duty staff to locate which connection /
        #   which alert; a line that only logs is **unknown externally** -- the dashboard alert stream
        #   is driven by the PUBLISH below, so when persistence fails but publish succeeds, the frontend
        #   alert still scrolls, and the dashboard is green with a clear conscience.
        # - The Counter lets Prometheus use increase(...[5m]) to judge "currently failing",
        #   paired with the AlertStoreFailing rule in robot-alerts.yml -> a real "alert about alerts".
        metrics.store_errors.inc()
        log.exception("alert insert failed: %s/%s", alert.robot_id, alert.rule_id)
        alert_id = None

    try:
        r = await get_redis()
        # Envelope structure unchanged (forbidden item 3): still flat JSON, still type:"alert" data.
        # Two new **data fields** added (no new envelope type):
        #   alert_id -- the addressing key for frontend ack/unack;
        #   escalated -- always false on first send, so the frontend renders the field uniformly (avoids undefined branch).
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


async def save_suppressed(alert: Alert, record: InhibitionRecord) -> None:
    """Persist a **suppressed** alert (order 35), but do not publish or notify.

    [Three differences from `save()`, each deliberate]
    1. Do NOT PUBLISH `robot:alerts`: the semantics of suppression is "this does not go to the
       dashboard's real-time stream". If it were also sent out, the suppression layer would be visually
       non-existent (the frontend would still pop a new alert).
    2. `suppressed=TRUE` + `suppressed_by=record.source_rule`: the former is the REST contract's
       filter key (`GET /alerts?suppressed=false` is the default view), the latter is "who suppressed".
    3. `detail` merges `record.as_detail_fields()`: the severity comparison and reason are persisted
       together, so afterwards one need not dig through logs to answer "why didn't it go out".

    [Failure semantics consistent with `save()`] A persistence failure only records a metric + log,
    and is not thrown to the caller -- the suppression record's write failure **should not** bring down
    the alert chain (it is a side path in emit()).
    But it must leave a trace (Counter `store_errors` + exception log), otherwise "not in DB" and
    "no such alert at all" become indistinguishable again -- exactly the old debt noted in `save()`'s comment.

    [Why suppressed records also do not enter the batch queue] Alerts are low-cardinality events
    (one per robot every 60~120s after dedup); the same acquire path as `save()` suffices, no need to
    build another 500 rows/1s batch -- AGENTS.md §3's batch guardrail targets the 1000 msg/s **telemetry**,
    not alerts.
    """
    from datetime import datetime, timezone

    detail = dict(alert.detail or {})
    detail.update(record.as_detail_fields())

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.fetchval(
                SUPPRESSED_INSERT_SQL,
                datetime.now(timezone.utc),
                alert.robot_id,
                alert.rule_id,
                alert.severity,
                alert.message,
                json.dumps(detail),
                record.source_rule,
            )
    except Exception:
        metrics.store_errors.inc()
        log.exception(
            "suppressed alert insert failed: %s/%s", alert.robot_id, alert.rule_id
        )


async def load_online_state() -> dict[str, bool]:
    """Read robot_metadata's (robot_id -> is_online), as the primary criterion for the offline rule.

    ISSUE #96: the judgment source changed from "in-process memory inference" to "read the column
    written by the backend heartbeat". The backend `metadata_writer` is the authoritative writer of
    this column (it batch-persists on the telemetry-receiving chain), and this engine is only a reader --
    one `fetch` retrieves 100 rows, not amplified by robot count.

    **Return an empty dict on failure (rather than raising)**: the caller treats it as "column
    unavailable" and falls back to the last_seen time-delta judgment. This neither false-alarms offline
    nor brings down the whole ticker round.
    """
    try:
        # get_pool() is also inside try: pool-creation failure (network/PG jitter) and query failure
        # are the same class of "read fails -> fall back" situation, and should not bring down the
        # whole ticker round.
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT robot_id, is_online FROM robot_metadata")
    except Exception:
        log.exception("metadata online-state read failed")
        return {}
    return {r["robot_id"]: bool(r["is_online"]) for r in rows}


async def update_online(robot_id: str, online: bool) -> None:
    """Maintain robot_metadata.is_online / last_seen (only called on state change)."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE robot_metadata SET is_online=$2, last_seen=NOW() WHERE robot_id=$1",
                robot_id, online,
            )
    except Exception:
        log.exception("metadata update failed: %s", robot_id)
