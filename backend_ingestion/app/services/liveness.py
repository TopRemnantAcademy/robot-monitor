"""在线/失联判定（ISSUE #77）—— 后端是「谁还活着」的唯一事实源。

## 为什么必须有这个模块

此前「在线」由前端用 `status_code !== 0` 推断（`store/robotStore.ts` 旧
`selectStatusCounts`）。而 `status_code` 是**机器人自己上报的字段**，它一旦停止上报，
最后一次上报的值就冻结在那里 —— 于是断电 1 小时的机器人依然显示「在线」。
大屏「在线 100 / 离线 0」是纯假象（ISSUE #77 正文）。

本模块用**接收侧事实**（我们最后一次收到该死机器人遥测的时刻）替代上报侧自述：
- `liveness:robot:{id}`（Hash）—— 后端每次收到遥测时 `HSET`，字段 `last_seen`（epoch 秒）
  与 `status_code`（最后一帧的状态，供快照展示）。**TTL 不用 `settings.state_ttl_seconds`**：
  那会给「接收侧事实」绑一个 10s 过期，10s 后 `EXISTS` 为假，于是一条 10.1s 前的遥测
  会被读成「从未上报」，收到多少就蒸发多少（实测踩到过）。这里 TTL 是纯粹的
  **垃圾回收水位**，按失联阈值的 100 倍取（下限 1 小时），远大于判定窗口，
  不会与判定语义耦合。
- `liveness:last_seen`（ZSET）—— 成员 = robot_id，score = 接收时的 epoch 秒。
  排名在线数与 `GET /robots` 的批量补齐都从这里取，**无需** 100 次读。

## 为什么用 Redis 而不是写库

AGENTS.md §3 与 ISSUE #77 的约束都写着「不得引入每条遥测的额外写库（批量更新）」。
1000 msg/s 下逐条 `UPDATE robot_metadata` 就是 1000 次事务/秒。
所以热路径只碰 Redis（已在同一条 `write_and_publish` 里，O(1)）；
`robot_metadata.is_online/last_seen` 由 `metadata_writer` 在同一处**投递**，
再按 1s 折叠成单语句批量 UPDATE 落库（ISSUE #96）——
「收到一帧」与「台账更新」由此同源，不再有两个真相。

## 为什么不用 Redis Key 过期事件

`notify-keyspace-events` 默认关闭，且 key 过期后引擎反而读不到 `last_seen`
（`alert_engine/app/rules/offline.py` 顶部已记录同一结论）。故这里**只存不判**：
判定统一在读取时用 `now - last_seen > threshold` 派生，不依赖任何通知机制。
"""

from __future__ import annotations

import logging
import time

from app.config import settings
from app.db.redis import get_redis

log = logging.getLogger("liveness")

# ---- key 命名（全仓唯一事实源；测试与运维排查都引用这里）----
# fmt: off
KEY_PREFIX = "liveness:robot:"      # Hash：{last_seen, status_code}
KEY_ZSET = "liveness:last_seen"     # ZSET：member=robot_id, score=last_seen(epoch 秒)
KEY_ONLINE_COUNT = "liveness:online_count"  # String：score 落在判定窗口内的成员数（ZCOUNT 结果缓存）
# fmt: on


def _gc_ttl() -> int:
    """Redis Key 的 GC TTL（秒）——只做垃圾回收，不参与在线判定。

    `state_ttl_seconds`（10s）**不能**用在这里：它会把「我们最后一次收到遥测的时刻」
    在 10s 后抹掉，于是 10s 前上报过、之后停报的机器人会被读成「从未上报」。
    按失联阈值的 100 倍取（下限 1 小时），保证判定窗口内的事实一定读得到。
    """
    return max(3600, int(settings.robot_offline_threshold_s * 100))


async def record(robot_id: str, status_code: int) -> None:
    """记录「刚刚收到该机器人一帧遥测」。热路径（1000 msg/s）——只做 O(1) Redis 写。

    失败只记日志不抛：与 `state_cache.write_and_publish` 同一纪律 ——
    在线台账挂掉不该让采集循环停摆，代价只是大屏暂时保守显示。
    """
    try:
        r = await get_redis()
        now = time.time()
        ttl = _gc_ttl()
        pipe = r.pipeline()
        pipe.hset(
            f"{KEY_PREFIX}{robot_id}",
            mapping={"last_seen": str(now), "status_code": str(status_code)},
        )
        pipe.expire(f"{KEY_PREFIX}{robot_id}", ttl)
        pipe.zadd(KEY_ZSET, {robot_id: now})
        pipe.expire(KEY_ZSET, ttl)
        await pipe.execute()
    except Exception:
        log.exception("liveness record failed for %s", robot_id)


async def read(robot_id: str, *, now: float | None = None) -> dict:
    """读一台机器人的在线态。**未上报过 → 0 次 Redis 往返**（只做一次 EXISTS）。

    返回 `{"is_online": bool, "last_seen": int | None, "status_code": int | None}`。
    `is_online` 由后端派生并下发，前端不再自己拿 `status_code` 猜（ISSUE #77 验收）。
    """
    now = time.time() if now is None else now
    try:
        r = await get_redis()
        if not await r.exists(f"{KEY_PREFIX}{robot_id}"):
            return {"is_online": False, "last_seen": None, "status_code": None}
        h = await r.hgetall(f"{KEY_PREFIX}{robot_id}")
    except Exception:
        log.exception("liveness read failed for %s", robot_id)
        # 台账不可读时**保守判离线**：宁可漏报在线，也不要谎报在线。
        return {"is_online": False, "last_seen": None, "status_code": None}
    return _decode(h, now)


def _decode(h: dict[str, str], now: float) -> dict:
    last_seen = _to_float(h.get("last_seen"))
    status_code = _to_int(h.get("status_code"))
    return {
        "is_online": last_seen is not None and (now - last_seen) <= settings.robot_offline_threshold_s,
        "last_seen": None if last_seen is None else int(last_seen),
        "status_code": status_code,
    }


async def enrich(robots: list[dict], *, now: float | None = None) -> list[dict]:
    """给 `state_cache.snapshot()` 的输出批量补上 `is_online` / `last_seen`。

    100 台逐个 `read()` 是 200 次往返；这里用一条 pipeline（100×ZSCORE + 100×HGETALL）
    压到 2 个网络往返 —— `/robots` 是大屏与 Drawer 的高频读端点，不能按台数线性放大。

    ZSET 里没有 score = 从未上报 = 直接判离线，不再为它多打一次 Hash
    （init.sql 预置的 100 台开局几乎全是这个状态）。
    """
    now = time.time() if now is None else now
    ids = [x.get("robot_id") for x in robots if x.get("robot_id")]
    scores: dict[str, float] = {}
    hash_fields: dict[str, dict[str, str]] = {}
    try:
        r = await get_redis()
        pipe = r.pipeline()
        for rid in ids:
            pipe.zscore(KEY_ZSET, rid)
        for rid in ids:
            pipe.hgetall(f"{KEY_PREFIX}{rid}")
        out = await pipe.execute()
        for rid, score in zip(ids, out[: len(ids)]):
            if score is not None:
                scores[rid] = float(score)
        for rid, h in zip(ids, out[len(ids):]):
            if h:
                hash_fields[rid] = h
    except Exception:
        log.exception("liveness enrich failed, falling back to all-offline")

    for item in robots:
        rid = item.get("robot_id")
        last_seen = scores.get(rid)
        h = hash_fields.get(rid, {})
        if last_seen is None:
            # 从未上报：US-1 的「首屏离线卡」语义，保持不变。
            item["is_online"] = False
            item["last_seen"] = None
            continue
        item["is_online"] = (now - last_seen) <= settings.robot_offline_threshold_s
        item["last_seen"] = int(last_seen)
        # 最后一帧的 status_code（供卡片着色/温度显示）。Hash 里没有时保留 snapshot 自带值。
        sc = _to_int(h.get("status_code"))
        if sc is not None:
            item["status_code"] = sc
    return robots


async def online_count(*, now: float | None = None) -> int:
    """当前在线台数（ZSET 里 last_seen 落在判定窗口内的成员数）。

    `ZCOUNT` 的语义就是「score ∈ [min, max] 的成员数」，与逐台 `now - last_seen <= t`
    完全等价，但只花一次往返 —— 不必为「在线 37/100」这一个数字遍历 100 台。
    """
    now = time.time() if now is None else now
    try:
        r = await get_redis()
        return int(await r.zcount(KEY_ZSET, now - settings.robot_offline_threshold_s, "+inf"))
    except Exception:
        log.exception("liveness online_count failed")
        return 0


def _to_float(v: str | None) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(v: str | None) -> int | None:
    f = _to_float(v)
    return None if f is None else int(f)
