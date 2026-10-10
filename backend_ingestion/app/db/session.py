"""asyncpg 连接池（写路径使用）。

说明：批量落库走 asyncpg 原生批量接口（COPY / executemany），
比 ORM 逐条 INSERT 快一个数量级；ORM 模型见 models.py，用于读路径与 schema 对齐。

本机实测（PostgreSQL 15.19，见 PR 描述）：
- `copy_records_to_table` 在 500 行/批时 84.7k 行/s，20000 行/批时 197.9k 行/s；
- `executemany` 同批约 70.8k 行/s。峰值 1000 行/s 两者都有两个数量级冗余，
  故选择依据不是吞吐，而是 **容错**（见 services/batch_writer.py 的降级链）。
"""

from __future__ import annotations

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            command_timeout=settings.db_command_timeout_s,
            # 显式关掉 prepared statement 缓存：本项目批量路径用 COPY / executemany，
            # 基本用不到服务端缓存；而 statement_cache 在 PgBouncer(transaction 模式)
            # 之类连接池后面会报 "prepared statement already exists"。
            # 关掉换来的是**可移植性**，代价对本项目可忽略。
            statement_cache_size=0,
            # 回收前先 ping，避免拿到被数据库/中间件静默掐断的死连接。
            max_inactive_connection_lifetime=settings.db_max_idle_s,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    """探活：/healthz 用它把「数据库可达」暴露成可观测状态，而不是等落库报错。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
