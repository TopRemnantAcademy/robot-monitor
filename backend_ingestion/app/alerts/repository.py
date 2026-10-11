"""告警读写的 SQL 层（订单 17 T1）。

单独成文件而不是塞进 router：SQL 与 HTTP 是两种失败模式（前者报 DB 异常，
后者报 4xx/5xx），混在一起会让「分页 WHERE 拼错」这类问题只表现为 500，
排查时要在 FastAPI 栈里翻 SQL。这里保持纯 asyncpg 调用，可被单测直接驱动。

连接来源：`app/db/session.py:get_pool()` —— 与写路径**同一个池**，
不新开第二套连接（订单 15 禁止项 3 的同一原则）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.db.session import get_pool

log = logging.getLogger("alerts.repo")


def _decode_detail(v):
    """jsonb 列的规范化。

    asyncpg **默认不注册 jsonb codec**，`jsonb` 列取回来是 **str**（本机实测：
    同仓 batch_writer 的写路径也因此显式 `json.dumps`）。这里统一解成 dict，
    否则 `AlertItem.detail: dict | None` 会校验失败报 500 —— 这是 HTTP 测试
    真实抓到的问题，不是推测。
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    row["detail"] = _decode_detail(row.get("detail"))
    return row

# 显式列出列名（不用 SELECT *）：列序即响应字段序，且增列时不会悄悄改变契约。
_COLS = (
    "alert_id, time, robot_id, rule_id, severity, message, detail, "
    "ack, ack_at, ack_by, escalated"
)


def _build_filters(
    ack: bool | None,
    escalated: bool | None,
    robot_id: str | None,
) -> tuple[str, list[Any]]:
    """构造 WHERE 子句与参数。用 $n 占位符，杜绝字符串拼接注入。

    参数从 $1 起连续编号，调用方在此之后追加 limit/offset 参数时
    必须用 `len(params) + 1` 续号 —— 见 `list_alerts`。
    """
    clauses: list[str] = []
    params: list[Any] = []

    if ack is not None:
        params.append(ack)
        clauses.append(f"ack = ${len(params)}")
    if escalated is not None:
        params.append(escalated)
        clauses.append(f"escalated = ${len(params)}")
    if robot_id is not None:
        params.append(robot_id)
        clauses.append(f"robot_id = ${len(params)}")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def list_alerts(
    *,
    ack: bool | None,
    escalated: bool | None,
    robot_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """分页查询，返回 (items, total)。

    排序键是 `(time DESC, alert_id DESC)`：
    - `time DESC` 让最新的在前，符合告警流语义；
    - 同 `time` 可能有多条（一轮 tick 内多规则各报一条，见 init.sql:65 说明），
      故再按 `alert_id DESC` 兜底 —— 否则分页边界处顺序不稳定，
      「第 1 页最后一条」可能在下一页重复出现（经典的分页漂移）。
    """
    where, params = _build_filters(ack, escalated, robot_id)

    list_sql = (
        f"SELECT {_COLS} FROM robot_alert{where} "
        f"ORDER BY time DESC, alert_id DESC "
        f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    )
    count_sql = f"SELECT count(*) FROM robot_alert{where}"

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 同一连接、同一事务读两次：否则「列表」与「总数」之间若有人写入，
        # 会出现 total 与 items 对不上的自相矛盾响应。
        async with conn.transaction():
            rows = await conn.fetch(list_sql, *params, limit, offset)
            total = await conn.fetchval(count_sql, *params)

    return [_normalize(dict(r)) for r in rows], int(total or 0)


async def get_alert(alert_id: int) -> dict[str, Any] | None:
    """按 alert_id 取单条（ack/unack 前置存在性检查用）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM robot_alert WHERE alert_id = $1", alert_id
        )
    return _normalize(dict(row)) if row else None


async def set_ack(alert_id: int, ack: bool, ack_by: str | None) -> tuple[dict[str, Any] | None, bool]:
    """置 ack 状态，返回 (行, changed)。

    幂等由 SQL 自身保证，而不是"先 SELECT 判断再 UPDATE"：
    - `UPDATE ... WHERE alert_id = $1 AND ack IS DISTINCT FROM $2`
      —— 只在这一行状态确实不同时才更新。`changed` = `rowcount > 0`。
      重复 ack 时 rowcount=0，`ack_at`/`ack_by` **不被覆盖**（首次确认人得以保留，
      这才符合审计语义：谁第一个认领的）。
    - `RETURNING` 回填现值，故幂等命中时也拿得到 ack_at/ack_by 供响应体使用。

    `IS DISTINCT FROM` 而非 `<>`：`ack` 是非空列，这里用前者只是因为
    `ack_by`/`ack_at` 在 unack 时要置 NULL，语义上统一更安全（不会因 NULL 比较返回 UNKNOWN）。

    **ack_by 不接受空串**：JWT 的 `sub` 由调用方保证非空后传入；
    这里不做校验（校验在路由层，因它才知道"取不到 sub 该不该 500"）。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if ack:
                # 确认：写 ack_at/ack_by（仅在状态变化时）
                row = await conn.fetchrow(
                    f"""
                    UPDATE robot_alert
                       SET ack = TRUE, ack_at = NOW(), ack_by = $2
                     WHERE alert_id = $1 AND ack IS DISTINCT FROM TRUE
                    RETURNING {_COLS}
                    """,
                    alert_id, ack_by,
                )
            else:
                # 撤回：清 ack_at/ack_by（仅在状态变化时）
                row = await conn.fetchrow(
                    f"""
                    UPDATE robot_alert
                       SET ack = FALSE, ack_at = NULL, ack_by = NULL
                     WHERE alert_id = $1 AND ack IS DISTINCT FROM FALSE
                    RETURNING {_COLS}
                    """,
                    alert_id,
                )
            changed = row is not None
            if row is not None:
                return _normalize(dict(row)), changed
            # 幂等命中或行不存在 —— 由调用方查一次区分。这里直接返回现况。
            current = await conn.fetchrow(
                f"SELECT {_COLS} FROM robot_alert WHERE alert_id = $1", alert_id
            )
    return (_normalize(dict(current)) if current else None), changed
