"""告警引擎入口（spec/02 §5）。

两条驱动线：
1. **消费线**：订阅 Redis `robot:stream:all`，维护每台机器人滑动窗口，
   依次跑 motor_temp / battery_low / joint_stuck 三条规则。
2. **ticker 线**：每秒遍历 100 台机器人跑 offline 规则
   —— **不依赖 Redis Key 过期事件**（keyspace notification 默认关闭）。

告警经 deduplicator 过滤后由 store 写入 robot_alert 并 PUBLISH robot:alerts。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

from app.config import settings
from app.db import close_all, get_redis
from app.deduplicator import should_emit
from app.rules.battery import BatteryLowRule
from app.rules.joint_stuck import JointStuckRule
from app.rules.motor_temp import MotorTempRule
from app.rules.offline import OfflineRule
from app.rules.base import Alert
from app.store import save, update_online

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("alert_engine")

RULES = [MotorTempRule(), BatteryLowRule(), JointStuckRule(), OfflineRule()]
DEDUP_WINDOW = {r.rule_id: r.dedup_window_s for r in RULES}
STREAM_RULES = [r for r in RULES if r.rule_id != "offline"]
OFFLINE_RULE = next(r for r in RULES if r.rule_id == "offline")

windows: dict[str, deque] = {}
last_seen: dict[str, float] = {}
online_state: dict[str, bool] = {}

_tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """后台执行告警落库/发布，不阻塞消费循环；同时保留引用防止被 GC。"""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def emit(alert: Alert) -> None:
    window = DEDUP_WINDOW.get(alert.rule_id, 60)
    if await should_emit(alert, window):
        await save(alert)
        log.info("alert: %s %s sev=%d %s", alert.robot_id, alert.rule_id,
                 alert.severity, alert.message)


# ---------------------------------------------------------------- 消费线

async def consume() -> None:
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(settings.chan_stream_all)
    log.info("alert_engine consuming %s", settings.chan_stream_all)
    async for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        try:
            data = json.loads(msg["data"])
        except json.JSONDecodeError:
            continue
        rid = data.get("robot_id")
        if not rid:
            continue

        w = windows.setdefault(rid, deque(maxlen=settings.window_maxlen))
        w.append(data)
        last_seen[rid] = time.time()

        for rule in STREAM_RULES:
            try:
                alert = rule.evaluate(data, w)
            except Exception:
                log.exception("rule %s failed on %s", rule.rule_id, rid)
                continue
            if alert is not None:
                spawn(emit(alert))


# ---------------------------------------------------------------- ticker 线

async def ticker() -> None:
    """每秒一轮：跑 offline 规则 + 维护 robot_metadata 在线状态。"""
    while True:
        await asyncio.sleep(settings.tick_interval_s)
        now = time.time()
        for rid in settings.robot_ids:
            ls = last_seen.get(rid)
            w = windows.get(rid) or deque()
            try:
                alert = OFFLINE_RULE.evaluate({"robot_id": rid, "last_seen": ls}, w)
            except Exception:
                log.exception("offline rule failed on %s", rid)
                alert = None
            if alert is not None:
                spawn(emit(alert))

            online = ls is not None and (now - ls) <= settings.offline_after_s
            if online_state.get(rid) is not online:
                online_state[rid] = online
                spawn(update_online(rid, online))


async def amain() -> None:
    try:
        await asyncio.gather(consume(), ticker())
    finally:
        for t in list(_tasks):
            t.cancel()
        await asyncio.gather(*_tasks, return_exceptions=True)
        await close_all()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("alert_engine stopped")


if __name__ == "__main__":
    main()
