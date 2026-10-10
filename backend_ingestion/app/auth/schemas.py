"""认证相关的请求/响应模型（订单 15 T1）。

`UserRead` 显式列出对外暴露的字段：**不返回 `hashed_password`**，
这是 `SQLAlchemyBaseUserTable` 自带的字段，用 `model_config` 排除掉即可
（fastapi-users 的 `BaseUser` schema 已经这么做了，这里重新声明是为了
把 `role` 加进响应体 —— 前端要靠它隐藏管理员按钮）。
"""

from __future__ import annotations

from fastapi_users import schemas


class UserRead(schemas.BaseUser[int]):
    """当前用户信息。`role` 供前端做 UI 级权限裁剪（真正的判定在后端）。"""

    role: str


class UserCreate(schemas.BaseUserCreate):
    """注册请求。

    **刻意不接受 `role` 字段**：允许请求体指定角色等于开了一个自助提权后门
    （`POST /auth/register {"email":…, "role":"admin"}`）。
    `extra="forbid"` 让它显式报 422，而不是静默忽略。
    """

    model_config = {"extra": "forbid"}


class UserUpdate(schemas.BaseUserUpdate):
    """更新请求。同样禁止改 `role`（提权只能由运维改库）。"""

    model_config = {"extra": "forbid"}
