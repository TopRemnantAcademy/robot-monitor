"""Auth dependencies: REST role gate + WS token verification (order 15 T1~T3).

Three public entry points:
- `current_active_user`      -- any valid token (viewer-up). Guards read-only endpoints.
- `require_role(role)`       -- role factory returning a dependency function. Admin
  endpoints use it; viewer gets 403.
- `authenticate_ws_token()`  -- WS-specific: the `sub` decoded from `?token=`,
  **no DB lookup**.

The 401 vs 403 split (acceptance criteria distinguish them explicitly, do not
mix):
- No token / illegal or expired token -> **401** (framework authenticator raises).
- Valid token but insufficient role   -> **403** (`require_role` raises).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.backend import TOKEN_AUDIENCE, auth_backend, get_jwt_strategy
from app.auth.manager import UserManager, get_user_manager
from app.auth.roles import ALL_ROLES, Role
from app.auth.users import RobotUser
from app.config import settings

#: Extract the raw token from `Authorization: Bearer <token>`. `auto_error=False`
#: lets us decide ourselves what to report when the header is absent -- the
#: framework default is also 401, but explicit control avoids being blocked by
#: the framework's default when we later add whitelist/exemption logic.
_bearer = HTTPBearer(auto_error=False)


async def _load_user(token: str | None, user_manager: UserManager) -> RobotUser | None:
    """Verify signature + load user. On any failure return None uniformly; the
    caller decides 401.

    Here we call the strategy's `read_token` directly, not the `Authenticator`
    layer: `Authenticator`'s value is multi-backend polling (cookie / bearer /
    ...); this project has only JWT, and an extra layer is just one more place to
    chase framework versions.
    """
    if not token:
        return None
    strategy = get_jwt_strategy()
    return await strategy.read_token(token, user_manager)


async def current_active_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    user_manager: Annotated[UserManager, Depends(get_user_manager)],
) -> RobotUser:
    """Any valid token suffices (viewer-up). Guards read-only endpoints."""
    token = credentials.credentials if credentials else None
    user = await _load_user(token, user_manager)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Cache on request.state: audit writers outside WS (order 17) take the
    # trusted sub from here, without re-decoding a token.
    request.state.user = user
    return user


def require_role(role: Role) -> Callable[..., Awaitable[RobotUser]]:
    """Generate a FastAPI dependency requiring "at least some role".

    There are only two levels (order 15 T3 explicitly "two levels"), so this is
    "admin contains all of viewer", not peer permission bits. `ADMIN` requesting a
    `VIEWER` endpoint -> allowed; `VIEWER` requesting an `ADMIN` endpoint -> 403.
    This asymmetric relation is written out explicitly, so when a third role is
    added later nobody has to guess whether it should be included.
    """

    async def _dep(
        user: Annotated[RobotUser, Depends(current_active_user)],
    ) -> RobotUser:
        if not satisfies(user.role, role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{user.role}' lacks required role '{role.value}'",
            )
        return user

    return _dep


def satisfies(actual: str, required: Role) -> bool:
    """Role containment. An unknown role (hand-edited broken in DB) is always
    treated as unsatisfied, never allowed."""
    try:
        actual_role = Role(actual)
    except ValueError:
        return False
    if required is Role.VIEWER:
        return actual_role in ALL_ROLES  # any legal role can read
    return actual_role is Role.ADMIN


# ------------------------------------------------------------------ WS

async def authenticate_ws_token(token: str | None) -> str | None:
    """Verify WS `?token=`, return the JWT `sub`; any failure returns None.

    **Why no DB lookup here**: WS is a long connection; judging a role per frame
    after the handshake is impractical; the token is signed by this service with
    a signature and expiry, so local verification suffices to decide "one of us".
    Revocation immediacy is backstopped by the token lifetime (if order 17 needs
    instant revocation, add a Redis blacklist rather than a per-frame DB lookup
    here).

    `sub` is the source of the audit column's value (order 15 T5): the caller
    uses it to write `ack_by`, and **does not accept** an operator field from the
    frontend body/query (otherwise the audit is forgeable).
    """
    if not token:
        return None
    strategy = get_jwt_strategy()
    try:
        data = jwt.decode(
            token,
            strategy.decode_key,
            audience=[TOKEN_AUDIENCE],
            algorithms=[strategy.algorithm],
        )
    except jwt.PyJWTError:
        return None
    sub = data.get("sub")
    return sub if isinstance(sub, str) and sub else None
