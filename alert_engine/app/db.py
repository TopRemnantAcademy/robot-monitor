"""Redis / TimescaleDB connections for the alert engine (same directory as this service, independent of the backend).

The connection pool is constructed uniformly by `alert_engine/app/pool.py:create_pool()` -- which
encapsulates the asyncpg 0.32.0 semantic change of `min_size` from "pre-built count" to "idle floor"
(order 08 / I-6).
"""

from __future__ import annotations

import asyncio

import asyncpg
from redis.asyncio import Redis

from app.config import settings
from app.pool import create_pool

_pool: asyncpg.Pool | None = None
_redis: Redis | None = None
# Pool creation must be serialized: `_pool is None` is check-then-act, and `await create_pool(...)`
# yields the event loop -- concurrent callers all see `_pool is None`, so **each builds its own pool**.
#
# Local measurement (PostgreSQL 15.19): on the first ticker round, spawn one `update_online()` per
# 100 robots, 100 coroutines enter `get_pool()` simultaneously -> **100 pools** built,
# each pre-building `db_pool_size` connections -> far exceeding `max_connections`,
# 332 `TooManyConnectionsError`s. The failure state is self-contradictory (`pg_stat_activity` shows only 8
# connections, because the connections were never actually built); the root cause is the **number of pools**,
# not the pool size.
# Note (ISSUE #96): now the ticker calls `load_online_state()` **once per round** to fetch the whole-network
# online state (one round trip, not amplified by robot count), and `update_online()` is called infrequently
# only on state changes, so the concurrent pool-creation scenario above no longer reproduces; but this lock is
# a historical guardrail, and the alert persistence on the `consume()` side may still be concurrent
# (spawning multiple emits), so it is kept.
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # Double-check: while waiting for the lock, another caller may have already built the pool
        if _pool is None:
            _pool = await create_pool(
                dsn=settings.database_url,
                pool_size=settings.db_pool_size,
                command_timeout=settings.db_command_timeout,
                # Pool-build timeout: if not set, falls back to connection.connect()'s default 60s,
                # letting "DB not ready" hang the startup/liveness path (see the connect_timeout comment in
                # app/pool.py and the module header of backend_ingestion/app/db/session.py).
                connect_timeout=settings.db_connect_timeout_s,
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
