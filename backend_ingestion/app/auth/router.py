"""认证路由装配（订单 15 T1）。

对外契约（与派工单逐条对应）：
- `POST /auth/register`    —— 自助注册，**固定 viewer**（见 manager.on_after_register）
- `POST /auth/jwt/login`   —— 用户名=邮箱 + 密码 → `{"access_token", "token_type"}`
- `POST /auth/jwt/logout`  —— 无状态 JWT，服务端不存 token：
                             返回 204，真正的"登出"是前端丢弃 token。
                             保留该端点是为了给前端一个明确的语义出口，
                             而不是让前端自己"假装登出"。
- `GET  /users/me`         —— 返回当前用户（含 role），前端据此隐藏管理员按钮

角色提权不在本单范围：`/auth/register` 只发 viewer，把用户改成 admin 由运维
直接改库（`UPDATE robot_user SET role='admin' WHERE email=…`）。
本单不提供管理端点 —— 订单 15 没有这一项需求，加了就是多余的攻击面。
"""

from __future__ import annotations

from fastapi import APIRouter

from fastapi_users import FastAPIUsers

from app.auth.backend import auth_backend
from app.auth.manager import get_user_manager
from app.auth.schemas import UserCreate, UserRead, UserUpdate
from app.auth.users import RobotUser

fastapi_users = FastAPIUsers[RobotUser, int](get_user_manager, [auth_backend])

#: `GET /users/me` 用的依赖。任意有效角色均可访问（含 viewer）。
current_user = fastapi_users.current_user(active=True)

router = APIRouter()

# 注册 / 登录 / 登出。
# `get_register_router` 自带 `POST /auth/register`（前缀由 include_router 决定）。
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
