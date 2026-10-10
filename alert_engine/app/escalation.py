"""告警升级（escalation）—— 对齐 Alertmanager 的 `repeat_interval`。

问题：一条 severity=3 的告警发出去后，若值班员**没看到 / 没确认**，
它就永远停在告警流里不再发声 —— "告警发了但没人管"是监控系统最贵的失败模式。

语义（派工单 T2）：`severity=3` 且超过 `escalate_after_s`（默认 300s）仍未 `ack`
→ 置 `escalated=TRUE` 并**再次** `PUBLISH robot:alerts`（信封仍是 `type:"alert"`，
禁止项 3）。前端据此显示「⚠ 已升级」。

为什么是"扫描 DB"而不是"内存定时器"：
- 内存定时器在进程重启后全部丢失，重启即等于"取消所有升级"——报警系统的
  催办能力不能依赖进程存活。
- 告警**已经落库**（store.save），DB 是它的权威状态。`ack` / `escalated` 两列
  是持久事实，扫描它们得到的判定与重启无关，且多副本部署下天然一致。

重复次数：`alert:escalated:{alert_id}`（Redis 计数器，带 TTL）。
DB 的 `escalated` 只是布尔"升级过没有"，而"升级了几次"用 Redis 计数：
不建议为计数再加一列（订单 17 明确"禁止重复加列"的精神 —— 只加业务必需的列）。
计数器 TTL 设为升级阈值的若干倍，过期不影响语义（最多允许额外重发）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from app.config import settings
from app.db import get_pool, get_redis
from app import metrics

log = logging.getLogger("escalation")

#: 扫描 SQL。
#:
#: WHERE 谓词必须与 init.sql:206 的部分索引 `idx_alert_escalation` 逐字对齐
#: （`ack = FALSE AND escalated = FALSE AND severity = 3`），否则索引不被命中，
#: 每次扫描退化成对 robot_alert 的顺序扫描。
#:
#: 时间窗用 `time <= NOW() - escalate_after_s` 表达"已超时"。
#: `ORDER BY time` 让最早的超时告警先被处理（公平性），并配合 LIMIT 防单轮过长。
SCAN_SQL = """
SELECT alert_id, time, robot_id, rule_id, severity, message, detail
  FROM robot_alert
 WHERE ack = FALSE
   AND escalated = FALSE
   AND severity = 3
   AND time <= NOW() - make_interval(secs => $1)
 ORDER BY time
 LIMIT $2
"""

MARK_SQL = """
UPDATE robot_alert
   SET escalated = TRUE
 WHERE alert_id = $1 AND ack = FALSE AND escalated = FALSE
RETURNING alert_id
"""

#: 单轮最多处理的条数：防止积压时一次扫描把事件循环占满（每轮做 N 次 UPDATE + N 次 PUBLISH）。
SCAN_BATCH = 200


def _repeat_key(alert_id: int) -> str:
    return f"alert:escalated:{alert_id}"


async def escalate_once() -> int:
    """扫描一轮并升级，返回本轮升级条数。纯函数式：便于测试直接驱动单轮。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(SCAN_SQL, settings.escalate_after_s, SCAN_BATCH)
        if not rows:
            return 0
        escalated = await _escalate_rows(conn, rows)
    return escalated


async def _escalate_rows(conn, rows) -> int:
    """在一条连接上逐条升级（保证扫描与 UPDATE 在同一连接语义下）。"""
    r = await get_redis()
    escalated = 0
    for row in rows:
        alert_id = int(row["alert_id"])
        try:
            # 用 Redis 计数器做"最多重发 N 次"的护栏。INCR 是原子的，
            # 多副本并发扫描同一条时也只有一个副本拿到 "1"。
            n = await r.incr(_repeat_key(alert_id))
            if n == 1:
                # 首次计数：设一个长于阈值的 TTL，避免 Key 无限堆积。
                await r.expire(
                    _repeat_key(alert_id),
                    max(60, int(settings.escalate_after_s) * (settings.escalate_max_repeat + 2)),
                )
            if n > settings.escalate_max_repeat:
                # 已达重发上限：只计数不再重发，避免"没人管 → 永远刷屏"。
                log.warning(
                    "escalation capped: %s %s alert_id=%s repeats=%d",
                    row["robot_id"], row["rule_id"], alert_id, n,
                )
                continue

            # 条件 UPDATE：`ack = FALSE AND escalated = FALSE` 保证并发下
            # 只有一个副本能把这条翻成 TRUE（其它副本 rowcount=0 → 跳过重发）。
            marked = await conn.fetchrow(MARK_SQL, alert_id)
            if marked is None:
                continue

            await _publish_escalated(row)
            metrics.alerts_escalated.labels(rule_id=row["rule_id"]).inc()
            escalated += 1
            log.warning(
                "alert ESCALATED: %s %s sev=%d alert_id=%s (未确认超过 %ds, repeat=%d)",
                row["robot_id"], row["rule_id"], row["severity"], alert_id,
                settings.escalate_after_s, n,
            )
        except Exception:
            log.exception("escalate failed: alert_id=%s", alert_id)
    return escalated


async def _publish_escalated(row) -> None:
    """重发到 `robot:alerts`。

    ⚠️ 信封**必须仍是 type:"alert"**（禁止项 3）：前端 `useRobotWebSocket`
    按 `type` 三分支，改类型会让升级告警落到 default 分支被丢弃。
    用 `escalated: true` 这个**数据字段**表达"这是一条升级重发"，而不是新增信封类型。
    """
    detail = row["detail"]
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    if not isinstance(detail, dict):
        detail = {}

    payload = {
        "robot_id": row["robot_id"],
        "rule_id": row["rule_id"],
        "severity": row["severity"],
        "message": row["message"],
        "detail": detail,
        "ts": int(time.time() * 1000),
        # 前端据此显示「⚠ 已升级」；同时保留 alert_id 供前端「确认」按钮寻址。
        "escalated": True,
        "alert_id": int(row["alert_id"]),
    }
    r = await get_redis()
    await r.publish(settings.chan_alerts, json.dumps(payload, separators=(",", ":")))


async def escalation_loop() -> None:
    """周期性扫描。与 consume / ticker 并发跑（见 main.amain）。"""
    log.info(
        "escalation loop started: after=%ds interval=%.1fs max_repeat=%d",
        settings.escalate_after_s, settings.escalate_scan_interval_s,
        settings.escalate_max_repeat,
    )
    while True:
        await asyncio.sleep(settings.escalate_scan_interval_s)
        try:
            await escalate_once()
        except Exception:
            # 单轮失败不能让整条升级线退出：DB 抖动恢复后下一轮继续。
            log.exception("escalation scan failed")
