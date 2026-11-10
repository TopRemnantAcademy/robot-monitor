"""UserManager: registration, login verification, and password hashing
(order 15 T1).

Only override the two things that must be customized; everything else inherits
from `BaseUserManager`:
- `on_after_register`: pin the new user's role to `DEFAULT_ROLE` (viewer).
- Password hashing: use the framework's default `PasswordHelper` (bcrypt), do
  not build our own.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, IntegerIDMixin

from app.auth.roles import DEFAULT_ROLE
from app.auth.users import RobotUser, get_user_db
from app.config import settings

log = logging.getLogger("auth.manager")


class UserManager(IntegerIDMixin, BaseUserManager[RobotUser, int]):
    """`IntegerIDMixin` matches `RobotUser`'s `BIGSERIAL` primary key."""

    # Token signing for password reset / verification email. Reuse the JWT secret,
    # do not introduce a second key (one more key = one more thing to rotate, to
    # inject, possibly misconfigure).
    reset_password_token_secret = settings.jwt_secret
    verification_token_secret = settings.jwt_secret

    async def on_after_register(self, user: RobotUser, request: Optional[Request] = None) -> None:
        # Newly registered users are fixed as viewer. Privilege escalation is a
        # separate action, not on the registration path.
        if not user.role:
            user.role = DEFAULT_ROLE.value
        log.info("user registered: id=%s role=%s", user.id, user.role)

    # Deliberately do not override on_after_login / on_after_forgot_password:
    # there is no email channel (this is an on-prem private deployment), so the
    # framework's empty default is enough.
    # Written here to make clear "this is an intentional omission", not a miss.


async def get_user_manager(
    user_db=Depends(get_user_db),
) -> UserManager:
    yield UserManager(user_db)
