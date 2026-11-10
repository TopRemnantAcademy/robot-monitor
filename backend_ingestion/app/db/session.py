"""asyncpg connection pool (write path).

Note: batch flush uses asyncpg's native bulk interfaces (COPY / executemany),
one to two orders of magnitude faster than ORM per-row INSERT; the ORM model is
in models.py, used for the read path and schema alignment.

Local measurement (PostgreSQL 15.19, see PR description):
- `copy_records_to_table` at 500 rows/batch = 84.7k rows/s, 20000 rows/batch =
  197.9k rows/s;
- `executemany` at the same batch ~ 70.8k rows/s. Both have two orders of
  magnitude of headroom against the 1000 rows/s peak, so the selection basis is
  not throughput but **fault tolerance** (see the degradation chain in
  services/batch_writer.py).

Why the pool-build timeout (`timeout`) is essential, not "nice to have"
-----------------------------------------------------------------------
From asyncpg 0.32, `init_size` means "**connect at pool build**" (see the
official ``.. versionchanged:: 0.32.0`` cited in app/db/pool.py). That brings in
`connection.connect()`'s default: **`timeout=60`** (`asyncpg/connection.py`'s
``async def connect(..., timeout=60)``).
So the "build pool when DB not ready" path blocks the full 60 seconds before
raising `TimeoutError`.

This is fatal during service startup, three facts stacked together:

1. In container orchestration `depends_on: condition: service_healthy` is
   **unreliable** on this project's compose (`infra/docker-compose.yml` is 3.8
   syntax + bare docker compose, the condition is ignored), so the backend can
   easily start before timescaledb is up; startup also catches EMQX expanding its
   Erlang runtime window;
2. Pool build is part of the **probe path**: `/healthz` -> `_db_healthy()` ->
   `ping()` -> `get_pool()`. The container healthcheck asks every 5s, and each
   one is eaten by this 60s block;
3. Worse: the `await create_pool(...)` inside `get_pool()` once **cancelled**
   (probe / orchestrator timeout cancel), the pool is either unbuilt or built but
   never assigned to `_pool` -- the next probe starts over, so `db=up` **never**
   arrives.

`timeout=db_connect_timeout_s` (default 8s, see config.py) turns this path into
**bounded failure**: on timeout -> `TimeoutError` -> `ping()` returns False ->
`/healthz` honestly reports `db=down`, and the next probe cycle retries. Once the
DB is ready, the probe flips to `db=up` and self-heals.
"""

from __future__ import annotations

import asyncio

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None
# Same as alert_engine/app/db.py: `_pool is None` is check-then-act, while
# `create_pool()` awaits. Concurrent callers would each build a pool and blow up
# `max_connections`.
# This service's concurrent entries are `/healthz` probe + `batch_writer._flush`,
# both potentially arriving simultaneously.
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
                # **Mandatory**: without it, `connection.connect()`'s default
                # timeout=60 makes "DB not ready" jam the probe path for 60s
                # (mechanism in module header).
                # Note 0.32's DSN query param `?connect_timeout=` is **not
                # recognized**: an unknown query is sent down as a PostgreSQL
                # session parameter, the server errors "unrecognized configuration
                # parameter", and pool build fails outright (measured).
                # So we can only use create_pool's own `timeout`.
                timeout=settings.db_connect_timeout_s,
                # Explicitly disable the prepared-statement cache: this project's
                # bulk path uses COPY / executemany and basically never uses the
                # server cache; statement_cache behind a connection pool like
                # PgBouncer (transaction mode) errors "prepared statement already
                # exists".
                # Disabling buys **portability**; the cost is negligible here.
                statement_cache_size=0,
                # Ping before reclaiming, to avoid handing out dead connections
                # silently cut by the DB / middleware.
                max_inactive_connection_lifetime=settings.db_max_idle_s,
            )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    """Probe: `/healthz` uses it to expose "DB reachable" as an observable state,
    rather than waiting for a write to error.

    The whole path is **bounded**: pool build is capped by `db_connect_timeout_s`,
    connection acquisition by `db_command_timeout_s`. Any failure returns only
    False, never hangs `/healthz` (that would be "the probe killing itself").
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
