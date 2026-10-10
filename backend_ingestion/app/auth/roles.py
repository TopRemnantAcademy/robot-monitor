"""RBAC 角色常量（订单 15 T3）。

为什么要单独一个模块而不是写在 config.py：
`role` 既要进 JWT 签发侧（登录时写进 claims），又要在校验侧读出来做依赖注入，
两处必须引用**同一个**真值源。散落成字符串字面量的后果是"签的时候写 admin、
判的时候比 Admin"，且这种错不会在启动时报出来，只会让 admin 静默变 viewer。

权限模型（两级，够用就好，不加中间层）：
- VIEWER —— 操作员：只读。看板、100 台状态、单机历史、WS 实时流。
- ADMIN  —— 管理员：VIEWER 的全部 + 订单 17 的告警确认/静默等写操作。
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """`str` 混入让枚举值可直接被 PyJWT 序列化进 claims，无需手工转 str。"""

    VIEWER = "viewer"
    ADMIN = "admin"


#: 新注册用户的默认角色。**刻意不是 admin** ——
#: 自助注册只能拿到只读权，提权必须由运维改库或走管理接口，
#: 否则 `/auth/register` 就成了"任何人自己开管理员"的后门。
DEFAULT_ROLE = Role.VIEWER

#: 允许出现在 JWT 里的角色全集。签发前用它校验，挡住手写 claims 传入的越界值。
ALL_ROLES: frozenset[Role] = frozenset(Role)
