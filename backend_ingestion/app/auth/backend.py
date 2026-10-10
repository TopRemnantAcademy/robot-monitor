"""JWT 策略与认证后端（订单 15 T1/T3）。

角色怎么进 token：fastapi-users 的 `JWTStrategy.write_token()` 只写
`sub` 与 `aud`（已核 12.1.3 源码，见 `external/fastapi-users`… 本机实测：
`data = {"sub": str(user.id), "aud": self.token_audience}`）。
RBAC 需要 `role` 也在 claims 里，故这里**覆写 `write_token`** 追加 `role`，
而不是另起一套签发逻辑 —— 签名算法、有效期、audience 全部沿用框架实现，
只多一个字段，返回值与框架完全兼容。

为什么不覆写 `read_token`：判鉴权走 `read_token` 会每请求打一次库查用户，
而 WS 连接是长连接、REST 高频轮询，多一次查库没有收益。
`read_token` 保持框架原样（拿 `sub` 查用户），**权限判断放在 `deps.py`**
用已签名的 claims 完成，不额外查库。
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

#: JWT audience。沿用框架默认值，改它会让所有已签发 token 同时失效（无收益）。
TOKEN_AUDIENCE = "fastapi-users:auth"


class RoleAwareJWTStrategy(JWTStrategy[RobotUser, int]):
    """在标准 claims 之外带上 `role` 的 JWT 策略。"""

    async def write_token(self, user: RobotUser) -> str:
        data = {
            "sub": str(user.id),
            "aud": self.token_audience,
            # role 取自数据库里的用户行，不是请求体 —— 前端无法通过登录请求提权。
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


#: 登录/登出路由的传输方式。`tokenUrl` 指向登录端点，供 OpenAPI 文档的
#: Authorize 按钮使用；前端实际调用见 `components/LoginGate.tsx`。
bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")

auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)
