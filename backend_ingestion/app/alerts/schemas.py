"""Alert closed-loop request/response models (order 17 T1).

**Deliberately no `ack_by` field**: the acknowledging operator must come from
the JWT `sub`, never from a frontend body/query -- otherwise the audit is
forgeable (work-order forbidden item 2). The request body is therefore empty, and
the endpoints do not accept an explicit body schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AckRequest(BaseModel):
    """Ack/unack request body: **must be empty**.

    This model exists for one purpose only: make FastAPI return 422 instead of
    silently ignoring extra fields from the client. The most important field
    `extra="forbid"` blocks is `ack_by` -- if the frontend could pass it
    successfully, the audit column would be forgeable (work-order forbidden item
    2). After declaring the model with forbid,
    `POST /alerts/{id}/ack {"ack_by":"x"}` returns 422 (covered by the HTTP test).
    """

    model_config = {"extra": "forbid"}


class AlertItem(BaseModel):
    """`GET /alerts` list item. Fields map one-to-one to `robot_alert` columns
    (including order 17's escalated).

    Order 35 appends `suppressed` / `suppressed_by`: suppressed alerts are **also
    in this table**; the frontend uses them to mark "suppressed" in-row (lower
    visual weight, not hiding the row). This is the landing point of order 30's
    "suppression != silent discard" at the REST contract -- the suppression
    judgment and evidence still live in alert_engine; here we only read out its
    product.
    """

    alert_id: int
    time: datetime
    robot_id: str
    rule_id: str
    severity: int
    message: str
    detail: dict | None = None
    ack: bool
    ack_at: datetime | None = None
    ack_by: str | None = None
    escalated: bool
    #: Whether this row is suppressed (order 35). TRUE = persisted only, no
    #: stream: no push, no notification.
    suppressed: bool
    #: The root-cause rule that suppressed it (offline / motor_temp / ...). Paired
    #: with suppressed: must be non-null when suppressed=TRUE, None when FALSE.
    suppressed_by: str | None = None


class AlertPage(BaseModel):
    """Paginated response.

    `total` is the **total count under the same filter** (not affected by
    limit/offset), so the frontend can compute the total page count; without it
    the frontend would only know it reached the end by hitting an empty page.
    """

    items: list[AlertItem]
    total: int
    limit: int
    offset: int


class AckResult(BaseModel):
    """Ack/unack result.

    `changed=False` means an **idempotent hit** (the target row is already in the
    target state), not an error. The work-order requires "repeated ack is
    idempotent (no error, no re-write)", so we express it with 200 + changed=false
    rather than 409.

    `ack_at` / `ack_by` backfill the **current DB value**, so the frontend can
    immediately correct its local optimistic update.
    """

    alert_id: int
    ack: bool
    ack_at: datetime | None = None
    ack_by: str | None = None
    changed: bool = Field(description="whether state actually changed (false = idempotent hit)")
