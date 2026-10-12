"""asyncpg 连接池（写路径使用）。

说明：批量落库走 asyncpg 原生批量接口（COPY / executemany），
比 ORM 逐条 INSERT 快一个数量级；ORM 模型见 models.py，用于读路径与 schema 对齐。

本机实测（PostgreSQL 15.19，见 PR 描述）：
- `copy_records_to_table` 在 500 行/批时 84.7k 行/s，20000 行/批时 197.9k 行/s；
- `executemany` 同批约 70.8k 行/s。峰值 1000 行/s 两者都有两个数量级冗余，
  故选择依据不是吞吐，而是 **容错**（见 services/batch_writer.py 的降级链）。

建池超时（`timeout`）为什么是必需的，而不是「加着好看」
------------------------------------------------------
asyncpg 0.32 起 `init_size` 是「**建池即预连**」（见 app/db/pool.py 引的官方
``.. versionchanged:: 0.32.0``）。它把 `connection.connect()` 的默认值一并带进来：
**`timeout=60`**（`asyncpg/connection.py` 的 ``async def connect(..., timeout=60)``）。
于是「DB 还没就绪时建池」这条路会阻塞满 60 秒才抛 `TimeoutError`。

这在服务启动期是致命的，三个事实叠在一起：

1. 容器编排里 `depends_on: condition: service_healthy` 在本项目 compose 上**不可靠**
   （`infra/docker-compose.yml` 是 3.8 语法 + 裸 docker compose，该条件会被忽略），
   后端完全可能在 timescaledb 起来之前就先起来；启动期还赶上 EMQX 展开 Erlang 运行时的窗口；
2. 建池是**探活路径**的一部分：`/healthz` → `_db_healthy()` → `ping()` → `get_pool()`。
   容器 healthcheck 每 5s 问一次，每一次都被这个 60s 阻塞吃掉；
3. 更坏的是：`get_pool()` 里那句 `await create_pool(...)` 一旦**被取消**（探针/编排器的
   超时取消），池要么没建成、要么建成了却来不及赋值给 `_pool` —— 下一次探测从头再来，
   于是 `db=up` **永远**到不了。

`timeout=db_connect_timeout_s`（默认 8s，见 config.py）把这条路径变成**有界失败**：
超时即 `TimeoutError` → `ping()` 返回 False → `/healthz` 老实报 `db=down`，
下一个探测周期再试。DB 一旦就绪，探测即转为 `db=up` 并自愈。
"""

from __future__ import annotations

import asyncio

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None
# 同 alert_engine/app/db.py：`_pool is None` 是 check-then-act，而 `create_pool()`
# 会 await。并发调用方会各建一个池，把 `max_connections` 打爆。
# 本服务的并发入口是 `/healthz` 探活 + `batch_writer._flush`，两者都可能同时到达。
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=settings.db_pool_min_size,
                max_size=settings.db_pool_max_size,
                command_timeout=settings.db_command_timeout_s,
                # **必须有**：不给的话走 `connection.connect()` 的默认 timeout=60，
                # 让「DB 未就绪」把探活路径卡死 60s（机制见模块头注释）。
                # 注意 0.32 的 DSN query 参数 `?connect_timeout=` **不被识别**：
                # 未知 query 会被当成 PostgreSQL 会话参数下发，服务端报
                # `unrecognized configuration parameter`，直接建池失败（实测）。
                # 所以只能走 create_pool 自己的 `timeout`。
                timeout=settings.db_connect_timeout_s,
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
    """探活：/healthz 用它把「数据库可达」暴露成可观测状态，而不是等落库报错。

    整条路径都是**有界**的：建池受 `db_connect_timeout_s` 限制，取连接受
    `db_command_timeout_s` 限制。任一环节失败都只返回 False，
    绝不把 `/healthz` 拖成挂死（那是「探针自己把探针打挂」）。
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
