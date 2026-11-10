"""Offline buffer (competitive benchmark C: store-and-forward).

Inspired by Netdata (store-and-forward) and OpenRobOps (adaptive sampling),
which keep a blind spot-free history across network/DB outages. RobotMonitor's
`BatchWriter` previously *discarded* rows on a DB write failure (counted as
`rejected`). This module keeps the **original Telemetry JSON** of failed batches
in a Redis list and replays them back through `writer.put()` once the DB
recovers.

Design principles (same conservativeness as the existing engine)
-------------------------------------------------------------
- **Idempotent**: on replay, `ON CONFLICT DO NOTHING` means re-injecting
  duplicates is safe.
- **Lossless-first**: if the DB dies again mid-replay, the unprocessed items are
  restored to the buffer -> no data loss.
- **Safe fallback**: if Redis is unavailable, `push` fails and the caller counts
  the rows as `rejected` (the original behaviour). So the service never halts
  just because Redis is down.
- **Injectable client**: only a duck-typed `rpush / lrange / ltrim / delete /
  llen` is required, so tests run against an in-memory stub with no fakeredis.
"""

from __future__ import annotations

import asyncio
import logging

from app import metrics
from app.config import settings
from app.schemas.telemetry import Telemetry

log = logging.getLogger("offline_buffer")


class DurableBuffer:
    """Redis-compatible list backup queue (duck-typed client)."""

    def __init__(self, client, key: str, maxlen: int) -> None:
        self._c = client
        self._key = key
        self._maxlen = maxlen

    async def push(self, payload_json: str) -> None:
        await self._c.rpush(self._key, payload_json)
        # Trim the oldest entries if over capacity.
        if self._maxlen and await self._c.llen(self._key) > self._maxlen:
            await self._c.ltrim(self._key, -self._maxlen, -1)

    async def length(self) -> int:
        return await self._c.llen(self._key)

    async def replay(self, writer, limit: int = 1000) -> int:
        """Pull the whole buffer, re-validate, and re-inject via `writer.put()`.

        Returns the number of records successfully re-injected. Records that
        cannot be processed (parse failure / writer exception) are kept in the
        buffer so there is no loss if the DB dies again. The key is only deleted
        on successful completion.

        Lossless + duplicate-free: `lrange` does not pop, so on failure the
        buffer must be *replaced* with only the items still needing a retry
        (delete + re-push). A naive `rpush` of the remainder would leave the
        originals in place, causing duplicate re-injection / infinite retry.
        """
        items = await self._c.lrange(self._key, 0, -1)
        if not items:
            return 0
        ok = 0
        keep: list[str] = []          # original JSON still needing a retry
        for raw in items:
            try:
                t = Telemetry.model_validate_json(raw)
            except Exception:
                # Unparseable (cannot ever be written) -> permanently dropped.
                log.debug("offline buffer dropped unparseable payload")
                continue
            try:
                await writer.put(t)
                ok += 1
            except Exception:
                # DB down / writer error -> retry later.
                keep.append(raw)
        if keep:
            # Replace the buffer with only `keep` (no duplicates).
            await self._c.delete(self._key)
            for raw in keep:
                await self._c.rpush(self._key, raw)
        else:
            # All succeeded (or only the unparseable were dropped) -> clear key.
            await self._c.delete(self._key)
        return ok


async def run_replay(writer, buffer: DurableBuffer, interval_s: float) -> None:
    """Background replay loop. Skips entirely when the buffer is empty.

    `ping` is imported lazily to avoid a circular import with app.db.session.
    """
    from app.db.session import ping

    while True:
        try:
            await asyncio.sleep(interval_s)
            if await buffer.length() == 0:
                continue
            # Confirm the DB is alive again (recovery judgement).
            if not await ping():
                continue
            n = await buffer.replay(writer)
            if n:
                metrics.telemetry_buffered_replayed.inc(n)
                log.info("offline buffer replayed %d telemetry records", n)
        except Exception:
            log.exception("offline buffer replay failed")
