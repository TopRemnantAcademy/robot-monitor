"""告警引擎的 Redis / TimescaleDB 连接（与本服务同目录，独立于后端）。"""

from __future__ import annotations

import asyncpg
from redis.asyncio import Redis

from app.config import settings

_pool: asyncpg.Pool | None = None
_redis: Redis | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url, min_size=1, max_size=4, command_timeout=30
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
