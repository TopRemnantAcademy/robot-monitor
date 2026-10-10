"""认证与 RBAC（订单 15）。

分包理由：`main.py` 已经承载了 WS/REST 路由、lifespan 与指标三件事，
再塞进用户模型、JWT 策略与角色依赖会让它变成什么都往里放的杂物间。
这里只放认证相关的东西，`main.py` 通过 `auth.router` / `auth.deps` 两处接口接入。
"""
