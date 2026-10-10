"""鉴权依赖：REST 的角色门 + WS 的 token 校验（订单 15 T1~T3）。

三个对外入口：
- `current_active_user`      —— 任意有效 token（viewer 起）。护住只读端点。
- `require_role(role)`       —— 角色工厂，返回依赖函数。admin 端点用它，viewer 得到 403。
- `authenticate_ws_token()`  —— WS 专用：`?token=` 解出的 `sub`，**不查库**。

401 与 403 的分工（验收标准明确区分，不能混）：
- 没有 token / token 非法或过期 → **401**（框架 authenticator 抛）。
- token 有效但角色不够          → **403**（`require_role` 抛）。
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

#: 从 `Authorization: Bearer <token>` 取原始 token。`auto_error=False` 让我们
#: 自己决定"没带头"时报什么 —— 框架默认同样给 401，但显式控制便于将来加
#: 白名单/豁免逻辑时不被框架的默认行为挡住。
_bearer = HTTPBearer(auto_error=False)


async def _load_user(token: str | None, user_manager: UserManager) -> RobotUser | None:
    """验签 + 查用户。失败统一返回 None，由调用方决定 401。

    这里直接调策略的 `read_token`，不走 `Authenticator` 那一层：
    `Authenticator` 的价值是多后端轮询（cookie / bearer / …），
    本项目只有 JWT 一种，多一层封装只是多一处要跟着框架版本改的地方。
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
    """任意有效 token 即可（viewer 起）。护住只读端点。"""
    token = credentials.credentials if credentials else None
    user = await _load_user(token, user_manager)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 缓存到 request.state：WS 之外的审计写入方（订单 17）从这里取可信 sub，
    # 不必再解一次 token。
    request.state.user = user
    return user


def require_role(role: Role) -> Callable[..., Awaitable[RobotUser]]:
    """生成"至少需要某角色"的 FastAPI 依赖。

    只有两级（订单 15 T3 明确"两级"），所以是"admin 包含 viewer 的全部"，
    不是平级权限位。`ADMIN` 请求 `VIEWER` 端点 → 放行；
    `VIEWER` 请求 `ADMIN` 端点 → 403。这条不对称关系显式写出来，
    免得以后加第三个角色时有人去猜它该不该被包含。
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
    """角色包含关系。未知角色（库里被手工改坏）一律视为不满足，不放行。"""
    try:
        actual_role = Role(actual)
    except ValueError:
        return False
    if required is Role.VIEWER:
        return actual_role in ALL_ROLES  # 任何合法角色都能读
    return actual_role is Role.ADMIN


# ------------------------------------------------------------------ WS

async def authenticate_ws_token(token: str | None) -> str | None:
    """校验 WS 的 `?token=`，返回 JWT `sub`；任何失败返回 None。

    **为什么这里不查库**：WS 是长连接，握手后每帧判一次角色不现实；
    token 由本服务签发、带签名与过期时间，本地验签足以判定"是不是自己人"。
    撤销的即时性由 token 有效期兜底（订单 17 若需即时吊销，
    应在 Redis 加黑名单，而不是在这里每帧查库）。

    `sub` 是审计列的取值来源（订单 15 T5）：调用方拿它写 `ack_by`，
    **不接受**来自前端 body/query 的操作人字段（否则审计可伪造）。
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
