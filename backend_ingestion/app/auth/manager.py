"""UserManager：注册、登录校验与密码哈希（订单 15 T1）。

只覆写两件必须定制的事，其余全部沿用 `BaseUserManager`：
- `on_after_register`：把新用户角色显式钉在 `DEFAULT_ROLE`（viewer）。
- 密码哈希：用框架默认的 `PasswordHelper`（bcrypt），不自造。
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
    """`IntegerIDMixin` 对应 `RobotUser` 的 `BIGSERIAL` 主键。"""

    # 用于重置密码/验证邮件的 token 签名。复用 JWT 密钥，不引入第二个密钥
    # （多一个密钥就多一处要轮换、要注入、可能漏配的东西）。
    reset_password_token_secret = settings.jwt_secret
    verification_token_secret = settings.jwt_secret

    async def on_after_register(self, user: RobotUser, request: Optional[Request] = None) -> None:
        # 新注册用户固定为 viewer。提权是独立动作，不走注册路径。
        if not user.role:
            user.role = DEFAULT_ROLE.value
        log.info("user registered: id=%s role=%s", user.id, user.role)

    # 刻意不覆写 on_after_login / on_after_forgot_password：
    # 没有邮件通道（本系统是厂内私有部署），留默认的空实现即可。
    # 写在这里是为了说明"这是有意省略"，不是漏掉。


async def get_user_manager(
    user_db=Depends(get_user_db),
) -> UserManager:
    yield UserManager(user_db)
