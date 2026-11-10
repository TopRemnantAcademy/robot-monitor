"""Auth router assembly (order 15 T1).

External contract (maps line-by-line to the work order):
- `POST /auth/register`    -- self-service registration, **fixed viewer** (see
  manager.on_after_register)
- `POST /auth/jwt/login`   -- username=email + password ->
  `{"access_token", "token_type"}`
- `POST /auth/jwt/logout`  -- stateless JWT, server stores no token: returns 204,
  the real "logout" is the frontend discarding the token. Kept for a clear
  semantic outlet for the frontend, rather than letting it "fake logout".
- `GET  /users/me`         -- returns the current user (including role), so the
  frontend can hide admin buttons accordingly

Role escalation is out of scope for this ticket: `/auth/register` only issues
viewer; promoting a user to admin is done by ops editing the DB directly
(`UPDATE robot_user SET role='admin' WHERE email=…`).
This ticket provides no management endpoint -- order 15 has no such requirement,
and adding one is just extra attack surface.
"""

from __future__ import annotations

from fastapi import APIRouter

from fastapi_users import FastAPIUsers

from app.auth.backend import auth_backend
from app.auth.manager import get_user_manager
from app.auth.schemas import UserCreate, UserRead, UserUpdate
from app.auth.users import RobotUser

fastapi_users = FastAPIUsers[RobotUser, int](get_user_manager, [auth_backend])

#: Dependency for `GET /users/me`. Any valid role may access (including viewer).
current_user = fastapi_users.current_user(active=True)

router = APIRouter()

# Register / login / logout.
# `get_register_router` brings its own `POST /auth/register` (prefix decided by
# include_router).
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/auth",
    tags=["auth"],
)
router.include_router(
    fastapi_users.get_auth_router(auth_backend),
    prefix="/auth/jwt",
    tags=["auth"],
)
router.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/users",
    tags=["users"],
)
