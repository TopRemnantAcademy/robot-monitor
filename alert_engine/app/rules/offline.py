"""离线检测规则（spec/02 §3.1）—— **必须由独立 ticker 驱动**。

严禁依赖 Redis Key 过期事件：
- Redis 的 keyspace notification（`notify-keyspace-events`）**默认关闭**；
- 且 TTL 到期后 `robot:state:{id}` 已被删除，引擎反而读不到 last_seen。

故由 main.py 的 ticker 每秒遍历 100 台机器人。

判据来源（ISSUE #96）
---------------------
**优先读 `robot_metadata.is_online`**，不再在本进程用内存字典自行推算。
该列由后端 `backend_ingestion/app/services/metadata_writer.py` 心跳批量维护：
每条遥测把 `last_seen=服务端时刻 / is_online=TRUE`，超过 `state_ttl_seconds`
无遥测则由同一模块周期置 FALSE。它是「服务端最后一次看到这台机器人」的唯一
事实来源，本引擎再推一遍就等于制造第二个真相（两个进程的时钟、重启窗口、
Redis 抖动各自造出不同判定 → 看板说在线、告警说离线）。

**迁移期回落**：`is_online` 可能是 None —— 该列尚未被写入（部署初期表里还没人
写心跳，或本引擎读不到该行）。此时**回落到 last_seen 时间差**判定，而不是直接
判离线；否则会把「列还没写」误报成「100 台全离线」。回落后仍按 `offline_after_s`
阈值，语义与 #96 之前的版本**逐字节一致**。
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
        self.use_is_online = settings.offline_use_is_online

    # ---- 复用同一份告警构造，避免两条路径的文案/字段漂移 ----
    def _alert(self, rid: str, last_seen: float | None) -> Alert:
        if last_seen is None:
            return Alert(
                robot_id=rid,
                rule_id=self.rule_id,
                severity=self.severity,
                message="机器人离线：从未上报过遥测",
                detail={"age_s": None},
            )
        age = time.time() - last_seen
        return Alert(
            robot_id=rid,
            rule_id=self.rule_id,
            severity=self.severity,
            message=f"机器人离线：已 {age:.1f}s 无遥测（阈值 {self.after_s}s）",
            detail={"age_s": round(age, 1), "threshold_s": self.after_s},
        )

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        """state 由 ticker 构造：{"robot_id", "is_online": bool|None, "last_seen": epoch|None}。

        判定优先级（`offline_use_is_online=True` 时）：
          1. `is_online is True`  → 不报（后端明确说它在线上）
          2. `is_online is False` → 报（后端明确说它离线）—— 即使内存 last_seen 很新，
                                    也以列为准（这正是「判据单源」的证据）
          3. `is_online is None`  → 列不可用，**回落**到 last_seen 时间差判定
        """
        rid = state.get("robot_id", "")
        last_seen = state.get("last_seen")
        is_online = state.get("is_online")

        if self.use_is_online and is_online is not None:
            if is_online:
                return None
            # 后端判定离线：消息里带 last_seen 供人判断是「从未上报」还是
            # 「上报后掉线」，判定本身不依赖它。
            return self._alert(rid, last_seen)

        # 回落路径：列不可用（None）或开关关闭 → 用内存 last_seen 时间差。
        if last_seen is None:
            return self._alert(rid, None)
        age = time.time() - last_seen
        if age <= self.after_s:
            return None
        return self._alert(rid, last_seen)
