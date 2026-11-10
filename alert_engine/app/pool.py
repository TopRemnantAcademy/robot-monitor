"""asyncpg connection pool factory (order 08 / I-6, behavior guardrail for asyncpg 0.29.0 -> 0.32.0).

Same source as `backend_ingestion/app/db/pool.py` (the same semantic guardrail, kept as two copies
for the two services, to avoid an import dependency between them).

Background (before vs after upgrade, `min_size` semantic change):

- <= 0.31: `min_size` = the number of connections **pre-built at pool creation**; the pool may drain
  all the way to 0 while idle, and the next `acquire()` connects once on demand (the first query waits
  for one TCP + auth handshake).
- >= 0.32: `min_size` becomes a **connection floor** (N connections always maintained while idle, with
  reconnections topped up in the background); the original "pre-built N" semantics moved to the new
  parameter `init_size` (defaults to `min_size`).
  See asyncpg docs for `create_pool()`'s ``.. versionchanged:: 0.32.0``:
  "The *min_size* parameter now defines the connection floor. The former role of
  *min_size* -- setting the initial pool size -- is now handled by the new *init_size*
  parameter, which defaults to *min_size*."

What we want is the **union of both**: both pre-built AND a persistent floor (robots fire at 10Hz 7x24,
connections should not be reclaimed). So simply following the official migration advice
`min_size=N` -> `init_size=N, min_size=0` is **not enough**; we must also keep the floor:

    init_size=min_size, min_size=min_size   # pre-build N + keep N maintained while idle

This module pins this down with a single entry point, to avoid the two `create_pool()` call sites each
writing their own version and stepping on the same semantic pitfall again later.

Compatibility basis (verified locally, see PR description):
- asyncpg 0.32.0's `create_pool()` accepts both `init_size` and `min_size`
  (safe even when the version is not pinned: 0.29/0.31 simply ignore the unknown `init_size`).
- `Pool.get_init_size()` is new in 0.32.0, so it is not called here.
"""

from __future__ import annotations

import asyncpg

__all__ = ["create_pool", "MAX_POOL_SIZE"]

# Lower bound for `max_size`. **Key point: do not set it equal to `pool_size`.**
#
# Local measurement (asyncpg 0.32.0 / PostgreSQL 15.19): when all three values are set to N,
# under 40 concurrent `acquire()` the pool size is **always N**, and the excess coroutines all block on
# `_queue.get()`, the pool degrading into a semaphore. Proof by contradiction: with
# `init_size=2, min_size=2, max_size=10` the same scenario grows to 10.
#
# Measured cost (alert_engine): `emit()` spawns one task per alert; with the pool pinned at 2 connections,
# **368 alert writes threw `TooManyConnectionsError`, only 115 succeeded** -- silently dropping alerts.
#
# Lesson, same root as the `min_size` semantic change: in 0.32.0 `min_size` is a **floor**, not
# "pre-build N"; it is not responsible for leaving headroom for concurrency. Setting `max_size` equal to
# the floor is equivalent to declaring "this service has a concurrency degree of 1".
MAX_POOL_SIZE = 10


async def create_pool(
    *,
    dsn: str,
    pool_size: int,
    command_timeout: float = 30.0,
    connect_timeout: float = 8.0,
) -> asyncpg.Pool:
    """Create an asyncpg pool that is "pre-built N + keeps N maintained while idle".

    Args:
        dsn: connection string.
        pool_size: the number of pre-built connections (`init_size`) and the idle floor (`min_size`).
            **Must not double as `max_size`** -- see the long comment on `MAX_POOL_SIZE` below.
        command_timeout: server-side timeout for a single command.
        connect_timeout: overall timeout (seconds) for **pool creation**, default 8s.

    Note: `command_timeout` **cannot stop** the hang when `executemany()` is blocked by a lock
    (verified locally on PostgreSQL 15.19, see `batch_writer.py`'s `asyncio.wait_for`);
    it only covers the normal-statement path; do not stake all long-transaction guards on it.

    Why `connect_timeout` exists: `init_size` means "pre-connect on pool creation", which brings in
    `connection.connect()`'s default `timeout=60` -- if the DB is not ready when creating the pool,
    every call blocks for the full 60s. Any **liveness/startup** path hitting it will hang instead of
    failing fast. 8s is a compromise of "shorter than any probe period, yet tolerant enough for one TCP
    handshake retry". Measured: with `init_size=2` and everything unreachable, without this param it
    throws `TimeoutError` at 60.02s; with 8s it throws at 8.01s (see `tests/test_db_pool_timeout.py`).
    """
    return await asyncpg.create_pool(
        dsn=dsn,
        init_size=pool_size,   # 0.32.0: pre-built on pool creation
        min_size=pool_size,    # 0.32.0: floor; 0.29/0.31: pre-built (same semantics, safe)
        max_size=max(pool_size, MAX_POOL_SIZE),
        command_timeout=command_timeout,
        timeout=connect_timeout,
    )
