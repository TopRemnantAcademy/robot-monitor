"""告警闭环的请求/响应模型（订单 17 T1）。

**刻意不定义 `ack_by` 字段**：确认人必须来自 JWT 的 `sub`，
绝不允许前端 body/query 传入 —— 否则审计可伪造（派工单禁止项 2）。
请求体因此为空，端点也不接受显式 body schema。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AckRequest(BaseModel):
    """确认/撤回的请求体：**必须为空**。

    存在这个模型只有一个目的：让 FastAPI 在客户端多传字段时报 422 而不是静默忽略。
    `extra="forbid"` 挡住的最重要一例就是 `ack_by` —— 前端若能传成功，
    就等于审计列可伪造（派工单禁止项 2）。声明成模型并设 forbid 后，
    `POST /alerts/{id}/ack {"ack_by":"x"}` 返回 422（HTTP 测试已覆盖）。
    """

    model_config = {"extra": "forbid"}


class AlertItem(BaseModel):
    """`GET /alerts` 列表项。字段与 `robot_alert` 列一一对应（含订单 17 的 escalated）。

    订单 35 追加 `suppressed` / `suppressed_by`：被抑制的告警**也在这张表里**，
    前端据此在行内标「已抑制」（降视觉权重而非隐藏行）。这是订单 30
    「抑制不等于静默丢弃」在 REST 契约上的落点 —— 抑制的判定与凭证仍在
    alert_engine，这里只把它的产物读出来。
    """

    alert_id: int
    time: datetime
    robot_id: str
    rule_id: str
    severity: int
    message: str
    detail: dict | None = None
    ack: bool
    ack_at: datetime | None = None
    ack_by: str | None = None
    escalated: bool
    #: 本条是否被抑制（订单 35）。TRUE = 只落库不出流：没有推送、没有通知。
    suppressed: bool
    #: 压制它的根因规则（offline / motor_temp / ...）。与 suppressed 成对：
    #: suppressed=TRUE 时必非空，FALSE 时为 None。
    suppressed_by: str | None = None


class AlertPage(BaseModel):
    """分页响应。

    `total` 是**同筛选条件下的总条数**（不受 limit/offset 影响），
    前端据此算总页数；缺了它前端只能"翻到空页才知道到底了"。
    """

    items: list[AlertItem]
    total: int
    limit: int
    offset: int


class AckResult(BaseModel):
    """确认/撤回结果。

    `changed=False` 表示**幂等命中**（目标行已是目标状态），不是错误。
    派工单要求「重复 ack 幂等（不报错、不重复写）」，故用 200 + changed=false
    表达，而不是 409。

    `ack_at` / `ack_by` 回填**库中现值**，便于前端立即纠正本地乐观更新。
    """

    alert_id: int
    ack: bool
    ack_at: datetime | None = None
    ack_by: str | None = None
    changed: bool = Field(description="是否真的改了状态（false = 幂等命中）")
