"""REST/WS 响应契约（订单 18 · 契约即代码）。

【为什么需要这个文件】
FastAPI 只会把**声明了 response_model 的路由**写进 `/openapi.json`。
`app/main.py` 里的 `/robots`、`/robots/{id}/history`、`/healthz` 此前直接
`return {...}`（裸 dict），于是生成的 OpenAPI 里它们的响应 schema 是 `{}`
（实测：`{"application/json": {"schema": {}}}`）——
前端就算用 openapi-typescript 生成，也只会得到一个 `unknown`，
等于"契约即代码"这件事从未发生。

本模块把这些**已经在运行时真实返回**的字段固化成 Pydantic 模型，
供 `main.py` 的 `response_model=` 引用，从而让 `/openapi.json` 里
真正带上字段级契约。字段名逐字对齐现有返回值，**不改任何一处的对外行为**。

WS 三分支（snapshot / telemetry / alert）OpenAPI 管不到（WS 不在 OpenAPI 描述范围），
故另有 `docs/contracts/ws-envelope.schema.json` + 前端 ajv 校验，见订单 18 T4。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --------------------------------------------------------------- 关节字典

# 键为关节名（spec/00 §5.5 的 24 个标准键 + 兼容存量），值为 float。
# 之所以用 dict[str, float] 而不是逐键展开：后端 JSONB 列的真实形态就是任意键名映射，
# 逐键展开会生成 24 个 required 字段，与"字典长度 1~40"（spec/01 §3）的语义不符，
# 且模拟器/未来机型增减关节时会变成一次 breaking change 假警报。
JointDict = dict[str, float]


# --------------------------------------------------------------- /robots

class RobotTelemetryOut(BaseModel):
    """单台机器人最新遥测（与 `state_cache._decode()` 的返回逐字段一致）。

    来源是 Redis Hash `robot:state:{robot_id}` 经 `_decode()` 还原类型后的 dict
    （`backend_ingestion/app/services/state_cache.py:76`）。
    注意 `joint_positions` 必须在这份契约里 —— 它正是 STATUS.md §六第 2 条
    那次事故（Drawer 第三条折线恒空）缺的字段。
    """

    robot_id: str
    status_code: int = Field(description="0 离线 / 1 正常 / 2 预警 / 3 故障")
    error_code: int = 0
    battery_pct: float
    max_joint_temp: float
    joint_temperatures: JointDict
    joint_positions: JointDict
    joint_torques: JointDict
    ts: int = 0


class RobotListResponse(BaseModel):
    """`GET /robots` —— 100 台最新状态（WS 之外的兜底/首屏）。"""

    robots: list[RobotTelemetryOut]


# --------------------------------------------------------------- /robots/{id}/history

class HistoryPointOut(BaseModel):
    """`/history` 的单个时间桶（`main.py` history() 的 points[] 元素）。"""

    time: str = Field(description="ISO8601 时间桶起点")
    status_code: int
    battery_pct: float
    max_joint_temp: float
    joint_temperatures: JointDict
    joint_torques: JointDict
    joint_positions: JointDict


class RobotHistoryResponse(BaseModel):
    """`GET /robots/{robot_id}/history`（spec/00 §5.3）。"""

    robot_id: str
    resolution: str
    points: list[HistoryPointOut]


# --------------------------------------------------------------- /healthz

class HealthResponse(BaseModel):
    """`GET /healthz` 存活探针。

    `status` 表达**依赖可达性**而非进程存活，故 HTTP 恒 200（见 main.py 的 docstring）。
    字段与 `main.py` healthz() 的返回逐字一致，含 ISSUE #19 的丢行三元组。
    """

    status: str = Field(description="ok（DB/Redis 可达）| degraded（进程活着但依赖不可达）")
    db: str = Field(description="up | down")
    written: int
    flushes: int
    retries: int
    rejected: int
    queue_depth: int
    mqtt_received: int
    mqtt_dropped: int
    rows_submitted: int
    rows_written: int
    rows_skipped: int


# --------------------------------------------------------------- /metrics

class MetricsResponse(BaseModel):
    """`GET /metrics` 的**类型占位**。

    Prometheus exposition format 是 `text/plain; version=0.0.4`，不是 JSON，
    故此处仅用于在 OpenAPI 里显式声明一个 `text/plain` 响应体，
    避免该端点被推断成 JSON。真正的内容契约由 Prometheus 文本格式本身约束
    （指标名对齐 `infra/observability/rules/robot-alerts.yml`）。
    """

    pass
