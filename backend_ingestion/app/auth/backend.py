"""JWT strategy and auth backend (order 15 T1/T3).

How role gets into the token: fastapi-users' `JWTStrategy.write_token()` only
writes `sub` and `aud` (verified against 12.1.3 source, see `external/fastapi-
users`... local measurement: `data = {"sub": str(user.id), "aud":
self.token_audience}`). RBAC needs `role` in the claims too, so here we **override
`write_token`** to append `role`, rather than standing up a separate signing
logic -- signing algorithm, lifetime, and audience all inherit from the
framework, only one field is added, and the return value stays fully compatible
with the framework.

Why not override `read_token`: judging auth via `read_token` would hit the DB per
request to load the user, while WS connections are long-lived and REST is
high-frequency polling -- one more DB lookup buys nothing. `read_token` stays as
the framework's (loads user by `sub`); the **permission judgment lives in
`deps.py`** using the already-signed claims, no extra DB lookup.
"""

from __future__ import annotations

from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.authentication.strategy.jwt import generate_jwt

from app.auth.users import RobotUser
from app.config import settings

#: JWT audience. Keep the framework default; changing it would invalidate all
#: already-issued tokens at once (no benefit).
TOKEN_AUDIENCE = "fastapi-users:auth"


class RoleAwareJWTStrategy(JWTStrategy[RobotUser, int]):
    """JWT strategy carrying `role` in addition to the standard claims."""

    async def write_token(self, user: RobotUser) -> str:
        data = {
            "sub": str(user.id),
            "aud": self.token_audience,
            # role comes from the user row in the DB, not the request body -- the
            # frontend cannot escalate via the login request.
            "role": user.role,
        }
        return generate_jwt(
            data, self.encode_key, self.lifetime_seconds, algorithm=self.algorithm
        )


def get_jwt_strategy() -> RoleAwareJWTStrategy:
    return RoleAwareJWTStrategy(
        secret=settings.jwt_secret,
        lifetime_seconds=settings.jwt_lifetime_seconds,
        token_audience=[TOKEN_AUDIENCE],
    )


#: Transport for the login/logout routes. `tokenUrl` points at the login
#: endpoint for OpenAPI's Authorize button; the frontend's actual call is in
#: `components/LoginGate.tsx`.
bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")

auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)
