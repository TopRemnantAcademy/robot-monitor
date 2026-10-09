"""asyncpg 连接池（写路径使用）。

说明：批量落库走 asyncpg 原生批量接口（copy/executemany），
比 ORM 逐条 INSERT 快一个数量级；ORM 模型见 models.py，用于读路径与 schema 对齐。
"""

from __future__ import annotations

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url, min_size=2, max_size=10, command_timeout=30
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
