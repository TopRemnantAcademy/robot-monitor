"""asyncpg connection pool factory (order 08 / I-6, asyncpg 0.29.0 -> 0.32.0
behavior guardrail).

Background (before vs after the upgrade, `min_size` semantics change):

- <= 0.31: `min_size` = the number of connections **pre-created at pool build**;
  the pool can drop all the way to 0 while idle, and the next `acquire()` opens a
  fresh connection (the first query pays one TCP + auth handshake).
- >= 0.32: `min_size` becomes a **connection floor** (N connections are always
  maintained while idle, broken links are replenished in the background); the old
  "pre-create a few" role moved to the new parameter `init_size` (defaults to
  `min_size`). See asyncpg docs `create_pool()` ``.. versionchanged:: 0.32.0``:
  "The *min_size* parameter now defines the connection floor. The former role of
  *min_size* — setting the initial pool size — is now handled by the new *init_size*
  parameter, which defaults to *min_size*."

What we want is the **union of both**: both pre-created AND a resident floor
(robots emit at 10Hz 7x24, connections should not be reclaimed). So following the
official migration advice `min_size=N` -> `init_size=N, min_size=0` is **not
enough**; we also keep the floor:

    init_size=min_size, min_size=min_size   # pre-create N + keep N resident while idle

This module fixes the behavior behind a single entry point, so the two
`create_pool()` call sites do not each write their own version and re-hit the same
semantic trap later.

Compatibility basis (verified locally, see PR description):
- asyncpg 0.32.0's `create_pool()` accepts both `init_size` and `min_size`
  (safe even when `spec` does not pin the version: 0.29/0.31 simply ignore the
  unknown `init_size`).
- `Pool.get_init_size()` is new in 0.32.0, so it is not called here.
"""

from __future__ import annotations

import asyncpg

__all__ = ["create_pool", "MAX_POOL_SIZE"]

# Lower bound for `max_size`. **Key point: it must not equal `pool_size`.**
#
# Verified locally (asyncpg 0.32.0 / PostgreSQL 15.19): when all three values are
# set to N, under 40 concurrent `acquire()` the pool size stays **at N**, and the
# surplus coroutines all block on `_queue.get()` — the pool degenerates into a
# semaphore. Counterexample: `init_size=2, min_size=2, max_size=10` grows to 10
# in the same scenario.
#
# Measured cost (alert_engine): `emit()` spawns one task per alert; with the pool
# pinned at 2, **368 alert writes raised `TooManyConnectionsError`, only 115
# succeeded** — silently dropping alerts.
#
# Lesson, same source as the `min_size` semantic change: 0.32.0's `min_size` is a
# **floor**, not "pre-create a few" — it is not responsible for leaving concurrency
# headroom; setting `max_size` equal to the floor declares "this service's
# concurrency degree is 1".
MAX_POOL_SIZE = 10


async def create_pool(
    *,
    dsn: str,
    pool_size: int,
    command_timeout: float = 30.0,
    connect_timeout: float = 8.0,
) -> asyncpg.Pool:
    """Build an asyncpg pool that is "pre-create N + keep N resident while idle".

    Args:
        dsn: Connection string.
        pool_size: Pre-create count (`init_size`) and idle floor (`min_size`).
            **Must not also be used as `max_size`** — reason in the long comment
            on `MAX_POOL_SIZE` below.
        command_timeout: Per-command server-side timeout.
        connect_timeout: Overall **pool-build** timeout (seconds), default 8s.

    Note: `command_timeout` **cannot guard** against `executemany()` hanging when
    blocked on a lock (verified locally on PostgreSQL 15.19; see the
    `asyncio.wait_for` in `batch_writer.py`) — it only covers the normal statement
    path; do not stake the long-transaction guardrail entirely on it.

    Why `connect_timeout` exists: `init_size` means "connect at pool build", which
    drags in `connection.connect()`'s default `timeout=60` — building a pool when
    the DB is not ready blocks for the full 60s on every call. Any **probe/startup**
    path hitting it would hang rather than fail fast. 8s is a compromise: shorter
    than any probe period yet tolerant enough for one TCP handshake retry.
    Verified: with `init_size=2` and nothing connectable, without this arg it
    raises `TimeoutError` at 60.02s; with 8s it raises at 8.01s
    (see `tests/test_db_pool_timeout.py`).
    """
    return await asyncpg.create_pool(
        dsn=dsn,
        init_size=pool_size,   # 0.32.0: pre-create at pool build
        min_size=pool_size,    # 0.32.0: floor; 0.29/0.31: pre-create (semantics coincide, safe)
        max_size=max(pool_size, MAX_POOL_SIZE),
        command_timeout=command_timeout,
        timeout=connect_timeout,
    )
