"""告警落库 + 分发（spec/02 §5）。

- 写入 TimescaleDB `robot_alert` 超表
- **PUBLISH `robot:alerts`** —— 前端告警流的唯一数据源，缺此则前端永远空白

两段是**独立** try/except（刻意的：发不出去不该拦住落库，落库失败也不该拦住前端告警流）。
代价是二者会各自静默失败，所以 insert 失败必须同时递增
`robot_alert_store_errors_total`，否则「DB 里没有」与「根本没告警」无法区分。
"""

from __future__ import annotations

import json
import logging
import time

from app import metrics
from app.config import settings
from app.db import get_pool, get_redis
from app.inhibition import InhibitionRecord
from app.rules.base import Alert

log = logging.getLogger("store")

# RETURNING alert_id：WS 告警信封必须带上 alert_id，前端的「确认」按钮
# 才能寻址到具体一条（订单 17）。此前不返回 id，前端拿不到寻址键。
INSERT_SQL = """
INSERT INTO robot_alert (time, robot_id, rule_id, severity, message, detail)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
RETURNING alert_id
"""

# 被抑制告警的落库语句（订单 35）。
#
# 【为什么必须单独一条 SQL，而不能复用 INSERT_SQL】
# 抑制记录**不走 `save()`**：`save()` 的后半段会 PUBLISH `robot:alerts`，
# 而「不推给前端」正是抑制的语义 —— 复用它就等于把抑制退化成放行。
#
# 【为什么必须落库：订单 30 的「不静默丢弃」在此才真正闭环】
# 订单 30 已产出 `InhibitionRecord`（含 source/target/reason），并做了 WARNING 日志
# 与 `robot_alert_suppressed_total` 指标。但**告警一旦被抑制就不写库**，
# 于是 `GET /alerts` 里根本没有这行 —— 运维只能 grep 日志、数指标，
# 无法在告警台回答「这台机器当时还报了什么、被谁压住了」。
# 本语句把凭证转成一行 `robot_alert`（suppressed=TRUE），抑制因此可查、可审计、可呈现。
#
# detail 里塞入可追溯凭证（JSONB，无需新列）：`suppressed_by` 只是"谁压的"，
# 而 severity 对比与成因（offline / severity）属于取证信息，进 detail 最省 Schema。
# 合并发生在 Python 侧（`save_suppressed` 里 `dict(alert.detail)` + `update(...)`）——
# 不在 SQL 里写 `detail || $6::jsonb`：`alert.detail` 的旧值是该列**已存入**的内容，
# 而这里要合并的是规则**本次**产出的 detail，二者不是一回事（后者才是权威输入）。
SUPPRESSED_INSERT_SQL = """
INSERT INTO robot_alert
    (time, robot_id, rule_id, severity, message, detail, suppressed, suppressed_by)
VALUES ($1, $2, $3, $4, $5, $6::jsonb, TRUE, $7)
RETURNING alert_id
"""


async def save(alert: Alert) -> None:
    from datetime import datetime, timezone

    pool = await get_pool()
    try:
        # 走 acquire() 而不是 pool.execute()：前者失败时会**丢弃**这条连接，
        # 后者在连接处于异常状态（如网络中断）时可能被放回池中复用，导致后续写入连环失败。
        # 告警是低基数事件（去重后 60~120s 一条/机器人），这里多一层 acquire 无性能代价。
        async with pool.acquire() as conn:
            alert_id = await conn.fetchval(
                INSERT_SQL,
                datetime.now(timezone.utc),
                alert.robot_id,
                alert.rule_id,
                alert.severity,
                alert.message,
                json.dumps(alert.detail),
            )
    except Exception:
        # 指标 + 日志，缺一不可（G-4）：
        # - 日志留现场（堆栈、robot_id），供值班定位到具体是哪条连接/哪条告警；
        #   只打日志的行，**外部无人知晓**——看板的告警流由下面的 PUBLISH 驱动，
        #   落库失败但发布成功时，前端该告警照常滚动，看板绿得理直气壮。
        # - Counter 让 Prometheus 能用 increase(...[5m]) 判「正在失败」，
        #   并配上 robot-alerts.yml 的 AlertStoreFailing 规则 → 真正的告警的告警。
        metrics.store_errors.inc()
        log.exception("alert insert failed: %s/%s", alert.robot_id, alert.rule_id)
        alert_id = None

    try:
        r = await get_redis()
        # 信封结构不变（禁止项 3）：仍是扁平 JSON，仍是 type:"alert" 的 data。
        # 新增两个**数据字段**（不新增信封类型）：
        #   alert_id —— 前端 ack/unack 的寻址键；
        #   escalated —— 初发恒为 false，前端据此统一渲染字段（避免 undefined 分支）。
        await r.publish(settings.chan_alerts, json.dumps({
            "robot_id": alert.robot_id,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "message": alert.message,
            "detail": alert.detail,
            "ts": int(time.time() * 1000),
            "alert_id": alert_id,
            "escalated": False,
        }, separators=(",", ":")))
    except Exception:
        log.exception("alert publish failed: %s/%s", alert.robot_id, alert.rule_id)


async def save_suppressed(alert: Alert, record: InhibitionRecord) -> None:
    """把**被抑制**的告警落库（订单 35），但不发布、不通知。

    【与 `save()` 的三点差异，每一处都是刻意的】
    1. **不 PUBLISH** `robot:alerts`：抑制的语义就是"这条不推给看板实时流"。
       若也发出去，抑制层在观感上等于不存在（前端照旧弹一条新告警）。
    2. `suppressed=TRUE` + `suppressed_by=record.source_rule`：前者是 REST 契约的
       筛选键（`GET /alerts?suppressed=false` 才是默认视图），后者是"谁压的"。
    3. `detail` 合并 `record.as_detail_fields()`：把 severity 对比与 reason
       一并入库，事后无需再翻日志就能回答"为什么没出去"。

    【失败语义与 `save()` 一致】落库失败只记指标 + 日志，不抛给调用方 ——
    抑制记录的写入失败**不该**把告警链路带崩（emit() 里它是旁路）。
    但必须留痕（Counter `store_errors` + exception 日志），否则"库里没有"与
    "根本没这条告警"又变成不可区分 —— 正是 `save()` 注释里那条老账。

    【为什么抑制记录也不进批处理队列】告警是低基数事件（去重后 60~120s 一条/机器人），
    与 `save()` 走同一条 acquire 路径即可，无需再造一个 500 条/1s 的批处理 ——
    AGENTS.md §3 的批量护栏针对的是 1000 msg/s 的**遥测**，不是告警。
    """
    from datetime import datetime, timezone

    detail = dict(alert.detail or {})
    detail.update(record.as_detail_fields())

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.fetchval(
                SUPPRESSED_INSERT_SQL,
                datetime.now(timezone.utc),
                alert.robot_id,
                alert.rule_id,
                alert.severity,
                alert.message,
                json.dumps(detail),
                record.source_rule,
            )
    except Exception:
        metrics.store_errors.inc()
        log.exception(
            "suppressed alert insert failed: %s/%s", alert.robot_id, alert.rule_id
        )


async def load_online_state() -> dict[str, bool]:
    """读 robot_metadata 的 (robot_id → is_online)，作为 offline 规则的首要判据。

    ISSUE #96：判定来源从「本进程内存推算」改成「读后端心跳写入的列」。
    后端 `metadata_writer` 才是该列的权威写方（它在收到遥测的那条链路上批量落库），
    本引擎只做读者 —— 一次 `fetch` 取回 100 行，不按台数放大往返。

    **读不到时返回空字典（而不是抛错）**：调用方按「列不可用」处理，回落到
    last_seen 时间差判定。既不误报离线，也不把 ticker 整轮带崩。
    """
    try:
        # get_pool() 也放进 try：建池失败（网络/PG 抖动）与查询失败是同一类
        # 「读不到就回落」的情形，不该让 ticker 整轮带崩。
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT robot_id, is_online FROM robot_metadata")
    except Exception:
        log.exception("metadata online-state read failed")
        return {}
    return {r["robot_id"]: bool(r["is_online"]) for r in rows}


async def update_online(robot_id: str, online: bool) -> None:
    """维护 robot_metadata.is_online / last_seen（仅在状态变化时调用）。"""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE robot_metadata SET is_online=$2, last_seen=NOW() WHERE robot_id=$1",
                robot_id, online,
            )
    except Exception:
        log.exception("metadata update failed: %s", robot_id)
