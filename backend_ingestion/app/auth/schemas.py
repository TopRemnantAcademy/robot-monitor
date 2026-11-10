"""Auth-related request/response models (order 15 T1).

`UserRead` explicitly lists the fields exposed outward: **do not return
`hashed_password`**, which `SQLAlchemyBaseUserTable` carries by default; exclude
it via `model_config` (fastapi-users' `BaseUser` schema already does this; the
redeclaration here is to add `role` to the response body -- the frontend uses it
to hide admin buttons).
"""

from __future__ import annotations

from fastapi_users import schemas


class UserRead(schemas.BaseUser[int]):
    """Current user info. `role` lets the frontend do UI-level permission
    trimming (the real judgment is on the backend)."""

    role: str


class UserCreate(schemas.BaseUserCreate):
    """Registration request.

    **Deliberately does not accept a `role` field**: allowing the body to specify
    a role opens a self-service privilege-escalation backdoor
    (`POST /auth/register {"email":…, "role":"admin"}`).
    `extra="forbid"` makes it return 422 explicitly rather than silently ignoring.
    """

    model_config = {"extra": "forbid"}


class UserUpdate(schemas.BaseUserUpdate):
    """Update request. Also forbids changing `role` (escalation only by ops
    editing the DB)."""

    model_config = {"extra": "forbid"}
