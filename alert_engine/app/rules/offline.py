"""离线检测规则（spec/02 §3.1）—— **必须由独立 ticker 驱动**。

严禁依赖 Redis Key 过期事件：
- Redis 的 keyspace notification（`notify-keyspace-events`）**默认关闭**；
- 且 TTL 到期后 `robot:state:{id}` 已被删除，引擎反而读不到 last_seen。

故由 main.py 的 ticker 每秒遍历 100 台机器人，
用**进程内内存字典** last_seen 判定；机器人从未上线时 last_seen 为 None，同样判离线。
"""

from __future__ import annotations

import time
from collections import deque

from app.config import settings
from app.rules.base import Alert, Rule


class OfflineRule(Rule):
    rule_id = "offline"
    severity = 3                                  # critical
    dedup_window_s = settings.offline_dedup_s

    def __init__(self) -> None:
        self.after_s = settings.offline_after_s

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        # state 由 ticker 构造：{"robot_id": ..., "last_seen": epoch秒 或 None}
        last_seen = state.get("last_seen")

        if last_seen is None:
            return Alert(
                robot_id=state.get("robot_id", ""),
                rule_id=self.rule_id,
                severity=self.severity,
                message="机器人离线：从未上报过遥测",
                detail={"age_s": None},
            )

        age = time.time() - last_seen
        if age <= self.after_s:
            return None
        return Alert(
            robot_id=state.get("robot_id", ""),
            rule_id=self.rule_id,
            severity=self.severity,
            message=f"机器人离线：已 {age:.1f}s 无遥测（阈值 {self.after_s}s）",
            detail={"age_s": round(age, 1), "threshold_s": self.after_s},
        )
