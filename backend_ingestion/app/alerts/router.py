"""告警闭环 REST 端点（订单 17 T1）。

三个端点：
- `GET  /alerts`                 —— 分页列表（viewer 起可读，与 /robots 同级只读权限）
- `POST /alerts/{alert_id}/ack`  —— 确认（**需 admin**）
- `POST /alerts/{alert_id}/unack`—— 撤回（**需 admin**）

为什么 ack 写操作要 admin 而读不要：
确认是**有审计后果的写操作**（记录 ack_by = 具体操作人）。若 viewer 也能确认，
那么"谁认领了这条故障"就不再可信 —— 车间里任何一台巡检终端都能改状态。
读操作（看告警）不改变任何东西，viewer 起即可。

`ack_by` 只从 JWT `sub` 取（派工单禁止项 2）：
这里通过 `Depends(require_role(Role.ADMIN))` 拿到已验签的 `RobotUser`，
再从 `request.state.user`（`deps.py:current_active_user` 写入）取 `sub`。
**不接受** query/body 里的操作人字段。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.alerts import repository
from app.alerts.schemas import AckRequest, AckResult, AlertItem, AlertPage
from app.auth.deps import require_role
from app.auth.roles import Role
from app.auth.users import RobotUser

log = logging.getLogger("alerts.router")

router = APIRouter(tags=["alerts"])

#: 单页上限。与派工单示例（limit=50）一致的默认值，上限 500 的选取理由见下。
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def _subject(request: Request, user: RobotUser) -> str:
    """取审计主体（JWT `sub`）。

    优先 `request.state.user.id` —— 这是 `current_active_user` 查库拿到的真实用户主键；
    它与 JWT 的 `sub` 同源（fastapi-users 用 user id 作 sub，见 manager.py）。
    用 user.id 而不是再解一次 token：**避免"解出来的 sub"与"已鉴权的用户"可能不一致**
    （例如 token 在请求途中被吊销），审计写的是"确实通过了鉴权的那个人"。

    该值必非空：能走到这里说明鉴权已通过。
    """
    return str(user.id)


@router.get("/alerts", response_model=AlertPage)
async def list_alerts(
    ack: bool | None = Query(default=None, description="true/false 筛选确认状态；省略=全部"),
    escalated: bool | None = Query(default=None, description="true 只看已升级告警"),
    robot_id: str | None = Query(default=None, description="按机器人过滤"),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _user: RobotUser = Depends(require_role(Role.VIEWER)),
):
    """分页查询告警（解 H5）。

    只读，viewer 起可访问 —— 与 `GET /robots` 权限一致（看板必须所有人都能看）。
    典型用法：前端筛选「未确认」= `GET /alerts?ack=false&limit=50&offset=0`。
    """
    try:
        rows, total = await repository.list_alerts(
            ack=ack, escalated=escalated, robot_id=robot_id, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001 —— DB 不可达时回 503 而非 500
        log.exception("list_alerts failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"alert query failed: {exc}",
        ) from exc

    return AlertPage(
        items=[AlertItem(**r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/alerts/{alert_id}/ack", response_model=AckResult)
async def ack_alert(
    alert_id: int,
    request: Request,
    body: AckRequest | None = None,
    user: RobotUser = Depends(require_role(Role.ADMIN)),
):
    """确认告警。**幂等**：重复确认返回 200 + changed=false，不报错、不覆盖首次确认人。

    操作人（`ack_by`）只从 JWT 取，`body` 声明为 `AckRequest`（extra=forbid）
    仅为拒绝伪造字段；其内容**不被读取**。
    """
    return await _apply_ack(alert_id, True, _subject(request, user))


@router.post("/alerts/{alert_id}/unack", response_model=AckResult)
async def unack_alert(
    alert_id: int,
    request: Request,
    body: AckRequest | None = None,
    user: RobotUser = Depends(require_role(Role.ADMIN)),
):
    """撤回确认（误确认时用）。同样幂等：对未确认的告警再 unack 返回 changed=false。"""
    return await _apply_ack(alert_id, False, _subject(request, user))


async def _apply_ack(alert_id: int, ack: bool, subject: str) -> AckResult:
    """ack / unack 的公共实现。

    存在性检查与写入合并成一次 SQL（`set_ack` 的 RETURNING + 兜底 SELECT）：
    - 行存在且状态变化 → changed=True，回填新值；
    - 行存在但状态已是目标值 → changed=False，回填**现值**（幂等）；
    - 行不存在 → 404。

    这里区分「行不存在」与「幂等命中」靠 `set_ack` 返回的 row 是否为 None：
    两者都走兜底 SELECT，靠结果有无区分。
    """
    try:
        row, changed = await repository.set_ack(alert_id, ack, subject)
    except Exception as exc:  # noqa: BLE001
        log.exception("set_ack failed: alert_id=%s ack=%s", alert_id, ack)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ack update failed: {exc}",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"alert not found: {alert_id}",
        )

    return AckResult(
        alert_id=int(row["alert_id"]),
        ack=bool(row["ack"]),
        ack_at=row["ack_at"],
        ack_by=row["ack_by"],
        changed=changed,
    )
