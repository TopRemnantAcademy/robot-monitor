"""Alert closed-loop REST endpoints (order 17 T1).

Three endpoints:
- `GET  /alerts`                  -- paginated list (readable from viewer up, same as /robots read-only permission)
- `POST /alerts/{alert_id}/ack`   -- acknowledge (**requires admin**)
- `POST /alerts/{alert_id}/unack` -- retract (**requires admin**)

Why ack (a write) needs admin but read does not:
Acknowledging is a **write operation with audit consequences** (records ack_by =
the specific operator). If viewers could ack too, then "who claimed this fault"
would no longer be trustworthy -- any inspection terminal on the floor could
change state. Read operations (viewing alerts) change nothing, so viewer-up is
enough.

`ack_by` is taken only from the JWT `sub` (work-order forbidden item 2): here we
get the verified `RobotUser` via `Depends(require_role(Role.ADMIN))`, then read
`sub` from `request.state.user` (written by `deps.py:current_active_user`).
**Do not accept** an operator field from query/body.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.alerts import repository
from app.alerts.schemas import AckRequest, AckResult, AlertItem, AlertPage
from app.auth.deps import require_role
from app.auth.roles import Role
from app.auth.users import RobotUser

log = logging.getLogger("alerts.router")

router = APIRouter(tags=["alerts"])

#: Per-page cap. Default matches the work-order example (limit=50); the 500 cap's
#: rationale is below.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def _subject(request: Request, user: RobotUser) -> str:
    """Get the audit subject (JWT `sub`).

    Prefer `request.state.user.id` -- this is the real user primary key looked up
    from the DB by `current_active_user`; it is co-sourced with the JWT `sub`
    (fastapi-users uses the user id as sub, see manager.py). Using user.id rather
    than re-decoding the token **avoids "the decoded sub" and "the authenticated
    user" possibly disagreeing** (e.g. the token was revoked mid-request); the
    audit writes "the person who actually passed auth".

    This value must be non-empty: reaching here means auth passed.
    """
    return str(user.id)


@router.get("/alerts", response_model=AlertPage)
async def list_alerts(
    ack: bool | None = Query(default=None, description="true/false filter on ack status; omitted=all"),
    escalated: bool | None = Query(default=None, description="true = only escalated alerts"),
    suppressed: bool | None = Query(
        default=None,
        description="true = only suppressed / false = exclude suppressed; omitted=all (including suppressed)",
    ),
    robot_id: str | None = Query(default=None, description="filter by robot"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: RobotUser = Depends(require_role(Role.VIEWER)),
):
    """Paginated alert query (resolves H5).

    Read-only, accessible from viewer up -- same permission as `GET /robots`
    (the dashboard must be viewable by everyone). Typical usage: the frontend
    filters "unacked" with `GET /alerts?ack=false&limit=50&offset=0`.

    `suppressed` (order 35) is deliberately an **optional** parameter; when
    omitted it returns the full set (including suppressed rows).
    Why not default to "exclude suppressed": changing the default would silently
    change the behavior of existing callers (frontend alert stream, RobotDetailDrawer's
    200-row pull) without any code change. Leaving "does the default view hide
    suppressed rows" to the **caller's explicit expression** keeps the contract
    itself neutral.

    Note this is a different thing from "the frontend by default does not enter the
    unacked stream": the former is the API's neutrality, the latter is
    `AlertLogTable`'s display decision (default `suppressed=false` + a toggle).
    """
    try:
        rows, total = await repository.list_alerts(
            ack=ack, escalated=escalated, suppressed=suppressed,
            robot_id=robot_id, limit=limit, offset=offset,
        )
    except Exception as exc:  # noqa: BLE001 -- return 503 not 500 when DB unreachable
        log.exception("list_alerts failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"alert query failed: {exc}",
        ) from exc

    return AlertPage(
        items=[AlertItem(**r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/alerts/{alert_id}/ack", response_model=AckResult)
async def ack_alert(
    alert_id: int,
    request: Request,
    body: AckRequest | None = None,
    user: RobotUser = Depends(require_role(Role.ADMIN)),
):
    """Acknowledge an alert. **Idempotent**: re-acking returns 200 + changed=false,
    no error, no overwrite of the first ack-er.

    The operator (`ack_by`) is taken only from the JWT; `body` is declared as
    `AckRequest` (extra=forbid) only to reject forged fields; its content **is not
    read**.
    """
    return await _apply_ack(alert_id, True, _subject(request, user))


@router.post("/alerts/{alert_id}/unack", response_model=AckResult)
async def unack_alert(
    alert_id: int,
    request: Request,
    body: AckRequest | None = None,
    user: RobotUser = Depends(require_role(Role.ADMIN)),
):
    """Retract acknowledgement (for mis-acks). Also idempotent: unacking an
    already-unacked alert returns changed=false."""
    return await _apply_ack(alert_id, False, _subject(request, user))


async def _apply_ack(alert_id: int, ack: bool, subject: str) -> AckResult:
    """Common implementation of ack / unack.

    Existence check and write are merged into one SQL (`set_ack`'s RETURNING +
    fallback SELECT):
    - row exists and state changes -> changed=True, backfill new value;
    - row exists but state already the target -> changed=False, backfill **current
      value** (idempotent);
    - row does not exist -> 404.

    Distinguishing "row not found" from "idempotent hit" relies on whether the
    row returned by `set_ack` is None: both paths hit the fallback SELECT, and the
    presence of a result distinguishes them.
    """
    try:
        row, changed = await repository.set_ack(alert_id, ack, subject)
    except Exception as exc:  # noqa: BLE001
        log.exception("set_ack failed: alert_id=%s ack=%s", alert_id, ack)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ack update failed: {exc}",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"alert not found: {alert_id}",
        )

    return AckResult(
        alert_id=int(row["alert_id"]),
        ack=bool(row["ack"]),
        ack_at=row["ack_at"],
        ack_by=row["ack_by"],
        changed=changed,
    )
