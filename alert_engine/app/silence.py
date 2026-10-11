"""告警静默（silence）—— 对齐 Alertmanager `silence`。

语义：运维在检修/调试期间，希望某台机器人（或全部）**不要吵**。
静默期内命中的告警既不落库也不发布；TTL 到期自动恢复，无需人工解除。

Redis Key 约定（派工单 T3）：
- `alert:silence:{robot_id}`  —— 只静默单台机器人
- `alert:silence:all`         —— 静默全部（计划停机、批量调试）

为什么用 Redis TTL 而不是进程内状态：
1. 静默是"带截止时间的临时状态"，TTL 是它的自然表达 —— 到期自动消失，
   不会因为忘了解除而永久压制告警（这是进程内布尔标志最容易犯的错）。
2. alert_engine 可能多副本部署，静默必须跨副本生效，进程内状态做不到。

fail-open（Redis 不可达时**放行**）：
静默是"叠加在告警之上的便利"，不是数据正确性的一环。
Redis 挂了还压制告警，会让真正的故障彻底静默 —— 比"静默失效、告警变吵"危险得多。
与 `deduplicator.should_emit` 的 fail-open 是同一取舍。
"""

from __future__ import annotations

import logging

from app.db import get_redis
from app.rules.base import Alert

log = logging.getLogger("silence")

#: 全部静默的全局 Key。
SILENCE_ALL_KEY = "alert:silence:all"


def silence_key(robot_id: str) -> str:
    return f"alert:silence:{robot_id}"


async def set_silence(robot_id: str, ttl_s: int, all_robots: bool = False) -> None:
    """写入静默（带 TTL）。`all_robots=True` 或空 robot_id 时写全局 Key。

    接口形式保留在引擎侧（而非只在 Redis 里手工 SET），是为了让
    "静默"与"解除"有明确的代码入口，便于将来接 REST/CLI。
    """
    key = SILENCE_ALL_KEY if (all_robots or not robot_id) else silence_key(robot_id)
    r = await get_redis()
    await r.set(key, "1", ex=max(1, int(ttl_s)))
    log.info("silence set: %s ttl=%ds", key, ttl_s)


async def clear_silence(robot_id: str, all_robots: bool = False) -> None:
    """解除静默（不等 TTL 到期）。"""
    key = SILENCE_ALL_KEY if (all_robots or not robot_id) else silence_key(robot_id)
    r = await get_redis()
    await r.delete(key)
    log.info("silence cleared: %s", key)


async def is_silenced(alert: Alert) -> bool:
    """静默判定。命中全局 Key 或该机器人的 Key 即为静默。

    两次 GET 合并成一次 MGET：静默检查在**每条告警、每次 emit** 都跑，
    高峰期是热路径，两次往返网络 RTT 是纯粹浪费。
    """
    try:
        r = await get_redis()
        vals = await r.mget(SILENCE_ALL_KEY, silence_key(alert.robot_id))
        return any(v is not None for v in vals)
    except Exception:
        # fail-open：Redis 不可达时放行告警，绝不因静默层故障而压制故障告警。
        log.exception("silence check failed, allowing emit (fail-open)")
        return False
