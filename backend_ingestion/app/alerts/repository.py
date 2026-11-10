"""Alert read/write SQL layer (order 17 T1).

Separate file rather than stuffed into the router: SQL and HTTP are two failure
modes (the former reports DB exceptions, the latter 4xx/5xx); mixing them makes
problems like "wrong pagination WHERE" appear only as 500, and you have to dig
SQL out of the FastAPI stack. Here it stays pure asyncpg calls, directly
drivable by unit tests.

Connection source: `app/db/session.py:get_pool()` -- the **same pool** as the
write path, no second connection set (same principle as order 15 forbidden item 3).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.session import get_pool

log = logging.getLogger("alerts.repo")


def _decode_detail(v):
    """Normalize the jsonb column.

    asyncpg **does not register a jsonb codec by default**; a `jsonb` column comes
    back as a **str** (measured locally: this repo's batch_writer write path
    therefore explicitly `json.dumps`). Here we uniformly parse it to a dict,
    otherwise `AlertItem.detail: dict | None` would fail validation and 500 --
    this is a problem the HTTP test actually caught, not speculation.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    row["detail"] = _decode_detail(row.get("detail"))
    return row


# Explicit column list (no SELECT *): column order is the response field order,
# and adding columns will not silently change the contract.
# Order 35 appends suppressed / suppressed_by (column order matches init.sql, models.py).
_COLS = (
    "alert_id, time, robot_id, rule_id, severity, message, detail, "
    "ack, ack_at, ack_by, escalated, suppressed, suppressed_by"
)


def _build_filters(
    ack: bool | None,
    escalated: bool | None,
    suppressed: bool | None,
    robot_id: str | None,
) -> tuple[str, list[Any]]:
    """Build the WHERE clause and params. Use $n placeholders to eliminate string
    concatenation injection.

    Params are numbered contiguously from $1; when the caller appends
    limit/offset params after this, they must continue numbering with
    `len(params) + 1` -- see `list_alerts`.

    `suppressed` is **tri-state** (True/False/None=all), same shape as ack/
    escalated: the default view passes `false` to keep suppressed rows out of the
    unacked stream; turning on the "show suppressed" toggle makes the frontend
    pass `true` for a separate page (see AlertLogTable's dual-query impl).
    """
    clauses: list[str] = []
    params: list[Any] = []

    if ack is not None:
        params.append(ack)
        clauses.append(f"ack = ${len(params)}")
    if escalated is not None:
        params.append(escalated)
        clauses.append(f"escalated = ${len(params)}")
    if suppressed is not None:
        params.append(suppressed)
        clauses.append(f"suppressed = ${len(params)}")
    if robot_id is not None:
        params.append(robot_id)
        clauses.append(f"robot_id = ${len(params)}")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def list_alerts(
    *,
    ack: bool | None,
    escalated: bool | None,
    suppressed: bool | None,
    robot_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Paginated query, returns (items, total).

    Sort key is `(time DESC, alert_id DESC)`:
    - `time DESC` puts newest first, matching alert-stream semantics;
    - the same `time` may have multiple rows (several rules each report one within
      a tick, see init.sql:65 note), so `alert_id DESC` is a tiebreak -- otherwise
      pagination boundary order is unstable and "page 1's last row" may repeat on
      the next page (classic pagination drift).

    All filter conditions are assembled by `_build_filters` -- this function only
    handles pagination and total reconciliation.
    """
    where, params = _build_filters(ack, escalated, suppressed, robot_id)

    list_sql = (
        f"SELECT {_COLS} FROM robot_alert{where} "
        f"ORDER BY time DESC, alert_id DESC "
        f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    )
    count_sql = f"SELECT count(*) FROM robot_alert{where}"

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Same connection, same transaction, two reads: otherwise between "list"
        # and "total" someone could write, producing a self-contradictory response
        # where total and items disagree.
        async with conn.transaction():
            rows = await conn.fetch(list_sql, *params, limit, offset)
            total = await conn.fetchval(count_sql, *params)

    return [_normalize(dict(r)) for r in rows], int(total or 0)


async def get_alert(alert_id: int) -> dict[str, Any] | None:
    """Fetch one by alert_id (used by ack/unack for the existence check)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM robot_alert WHERE alert_id = $1", alert_id
        )
    return _normalize(dict(row)) if row else None


async def set_ack(alert_id: int, ack: bool, ack_by: str | None) -> tuple[dict[str, Any] | None, bool]:
    """Set ack state, return (row, changed).

    Idempotency is guaranteed by SQL itself, not by "SELECT-then-UPDATE":
    - `UPDATE ... WHERE alert_id = $1 AND ack IS DISTINCT FROM $2`
      -- only updates when this row's state actually differs. `changed` =
      `rowcount > 0`.
      On repeated ack rowcount=0, `ack_at`/`ack_by` are **not overwritten** (the
      first ack-er is preserved, which matches audit semantics: who claimed it
      first).
    - `RETURNING` backfills the current value, so an idempotent hit still gets
      ack_at/ack_by for the response body.

    `IS DISTINCT FROM` rather than `<>`: `ack` is a non-null column; here we use
    the former only because `ack_by`/`ack_at` are set to NULL on unack, and the
    semantics are uniformly safer (won't return UNKNOWN on a NULL comparison).

    **ack_by does not accept an empty string**: the JWT `sub` is guaranteed
    non-empty by the caller before passing in; this layer does not validate (that
    belongs to the router, because only it knows whether "can't get sub" should be
    a 500).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ack:
                # Acknowledge: write ack_at/ack_by (only on state change)
                row = await conn.fetchrow(
                    f"""
                    UPDATE robot_alert
                       SET ack = TRUE, ack_at = NOW(), ack_by = $2
                     WHERE alert_id = $1 AND ack IS DISTINCT FROM TRUE
                    RETURNING {_COLS}
                    """,
                    alert_id, ack_by,
                )
            else:
                # Retract: clear ack_at/ack_by (only on state change)
                row = await conn.fetchrow(
                    f"""
                    UPDATE robot_alert
                       SET ack = FALSE, ack_at = NULL, ack_by = NULL
                     WHERE alert_id = $1 AND ack IS DISTINCT FROM FALSE
                    RETURNING {_COLS}
                    """,
                    alert_id,
                )
            changed = row is not None
            if row is not None:
                return _normalize(dict(row)), changed
            # Idempotent hit or row not found -- the caller distinguishes with one
            # more query. Here we return the current state directly.
            current = await conn.fetchrow(
                f"SELECT {_COLS} FROM robot_alert WHERE alert_id = $1", alert_id
            )
    return (_normalize(dict(current)) if current else None), changed
