"""告警引擎的 Redis / TimescaleDB 连接（与本服务同目录，独立于后端）。

连接池由 `alert_engine/app/pool.py:create_pool()` 统一构造 —— 那里封装了
asyncpg 0.32.0 把 `min_size` 从"预建条数"改成"空闲地板"的语义变化（订单 08 / I-6）。
"""

from __future__ import annotations

import asyncio

import asyncpg
from redis.asyncio import Redis

from app.config import settings
from app.pool import create_pool

_pool: asyncpg.Pool | None = None
_redis: Redis | None = None
# 建池必须串行化：`_pool is None` 是 check-then-act，而 `await create_pool(...)`
# 会让出事件循环 —— 并发调用方全部看到 `_pool is None`，于是**各建一个池**。
#
# 本机实测（PostgreSQL 15.19）：ticker 首轮给 100 台机器人各 spawn 一个
# `update_online()`，100 个协程同时进 `get_pool()` → 建出 **100 个池**，
# 每个池预建 `db_pool_size` 条连接 → 远超 `max_connections`，
# 332 次 `TooManyConnectionsError`。故障态自相矛盾（`pg_stat_activity` 只有 8 条连接，
# 因为连接根本没建成），根因是**池的数量**而不是池的大小。
# 注（ISSUE #96）：现在 ticker 每轮**先调一次** `load_online_state()` 取回全网
# 在线态（1 次往返，不按台数放大），`update_online()` 仅在状态变化时低频调用，
# 上面的并发建池场景不复现；但这条锁是历史护栏，且 `consume()` 侧的告警落库
# 仍可能并发（spawn 多个 emit），故保留。
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # 二次检查：等锁期间可能已有调用方把池建好了
        if _pool is None:
            _pool = await create_pool(
                dsn=settings.database_url,
                pool_size=settings.db_pool_size,
                command_timeout=settings.db_command_timeout,
            )
    return _pool


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_all() -> None:
    global _pool, _redis
    if _pool is not None:
        await _pool.close()
        _pool = None
    if _redis is not None:
        await _redis.aclose()
        _redis = None
