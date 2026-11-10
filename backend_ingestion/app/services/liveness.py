"""Online / lost-contact judgment (ISSUE #77) -- the backend is the single
source of truth for "who is still alive".

## Why this module must exist

Previously "online" was inferred by the frontend from `status_code !== 0`
(`store/robotStore.ts`'s old `selectStatusCounts`). But `status_code` is a field
**reported by the robot itself**; once it stops reporting, the last reported
value freezes there -- so a robot powered off for an hour still showed "online".
The big-screen "online 100 / offline 0" was pure illusion (ISSUE #77 body).

This module replaces the self-reported side with **receive-side fact** (the
moment we last received telemetry from that robot):
- `liveness:robot:{id}` (Hash) -- the backend `HSET`s on every telemetry
  receipt, fields `last_seen` (epoch seconds) and `status_code` (the last
  frame's status, for snapshot display). **TTL does NOT use
  `settings.state_ttl_seconds`**: that would bind the receive-side fact to a 10s
  expiry; after 10s `EXISTS` is false, so telemetry from 10.1s ago would be read
  as "never reported" and evaporate however much we received (measured, hit this).
  Here TTL is purely a **GC water level**, taken at 100x the lost-contact
  threshold (floor 1 hour), far larger than the judgment window, so it never
  couples with the judgment semantics.
- `liveness:last_seen` (ZSET) -- member = robot_id, score = epoch seconds at
  receive. Both the online-count ranking and `GET /robots`'s batch fill-in read
  from here, **without** 100 reads.

## Why Redis rather than writing to the DB

Both AGENTS.md SS3 and ISSUE #77's constraints say "do not introduce a per-
telemetry extra DB write (batch update)". At 1000 msg/s, updating
`robot_metadata` per row is 1000 transactions/second. So the hot path only
touches Redis (already in the same `write_and_publish`, O(1));
`robot_metadata.is_online/last_seen` is **delivered** by `metadata_writer` at the
same spot, then folded into a single batched UPDATE at 1s cadence (ISSUE #96) --
"receiving a frame" and "ledger update" are thus co-sourced, no longer two truths.

## Why not Redis key-expiry events

`notify-keyspace-events` is off by default, and after a key expires the engine
cannot read `last_seen` anyway (the same conclusion is recorded at the top of
`alert_engine/app/rules/offline.py`). So here we **only store, never judge**: the
judgment is uniformly derived at read time via `now - last_seen > threshold`,
depending on no notification mechanism.
"""

from __future__ import annotations

import logging
import time

from app.config import settings
from app.db.redis import get_redis

log = logging.getLogger("liveness")

# ---- key naming (repo-wide single source of truth; tests and ops both reference here) ----
# fmt: off
KEY_PREFIX = "liveness:robot:"      # Hash: {last_seen, status_code}
KEY_ZSET = "liveness:last_seen"     # ZSET: member=robot_id, score=last_seen(epoch seconds)
KEY_ONLINE_COUNT = "liveness:online_count"  # String: count of members whose score is within the judgment window (ZCOUNT result cache)
# fmt: on


def _gc_ttl() -> int:
    """Redis key GC TTL (seconds) -- only garbage collection, not online judgment.

    `state_ttl_seconds` (10s) **cannot** be used here: it would erase "the moment
    we last received telemetry" after 10s, so a robot that reported 10s ago and
    then stopped would be read as "never reported". Taken at 100x the lost-contact
    threshold (floor 1 hour) to guarantee the in-window fact is always readable.
    """
    return max(3600, int(settings.robot_offline_threshold_s * 100))


async def record(robot_id: str, status_code: int) -> None:
    """Record "just received one telemetry frame from this robot". Hot path
    (1000 msg/s) -- only an O(1) Redis write.

    On failure only log, do not raise: same discipline as
    `state_cache.write_and_publish` -- if the online ledger is down, the ingest
    loop must not stop; the cost is only a temporarily conservative display.
    """
    try:
        r = await get_redis()
        now = time.time()
        ttl = _gc_ttl()
        pipe = r.pipeline()
        pipe.hset(
            f"{KEY_PREFIX}{robot_id}",
            mapping={"last_seen": str(now), "status_code": str(status_code)},
        )
        pipe.expire(f"{KEY_PREFIX}{robot_id}", ttl)
        pipe.zadd(KEY_ZSET, {robot_id: now})
        pipe.expire(KEY_ZSET, ttl)
        await pipe.execute()
    except Exception:
        log.exception("liveness record failed for %s", robot_id)


async def read(robot_id: str, *, now: float | None = None) -> dict:
    """Read one robot's online state. **Never reported -> 0 Redis round-trips**
    (only one EXISTS).

    Returns `{"is_online": bool, "last_seen": int | None, "status_code": int | None}`.
    `is_online` is derived and sent by the backend; the frontend no longer guesses
    from `status_code` (ISSUE #77 acceptance).
    """
    now = time.time() if now is None else now
    try:
        r = await get_redis()
        if not await r.exists(f"{KEY_PREFIX}{robot_id}"):
            return {"is_online": False, "last_seen": None, "status_code": None}
        h = await r.hgetall(f"{KEY_PREFIX}{robot_id}")
    except Exception:
        log.exception("liveness read failed for %s", robot_id)
        # When the ledger is unreadable, judge offline conservatively: better to
        # miss an online than to falsely claim online.
        return {"is_online": False, "last_seen": None, "status_code": None}
    return _decode(h, now)


def _decode(h: dict[str, str], now: float) -> dict:
    last_seen = _to_float(h.get("last_seen"))
    status_code = _to_int(h.get("status_code"))
    return {
        "is_online": last_seen is not None and (now - last_seen) <= settings.robot_offline_threshold_s,
        "last_seen": None if last_seen is None else int(last_seen),
        "status_code": status_code,
    }


async def enrich(robots: list[dict], *, now: float | None = None) -> list[dict]:
    """Batch-enrich `state_cache.snapshot()`'s output with `is_online` /
    `last_seen`.

    Per-robot `read()` for 100 units is 200 round-trips; here one pipeline
    (100x ZSCORE + 100x HGETALL) collapses to 2 network round-trips -- `/robots`
    is a high-frequency read endpoint for the big-screen and Drawer; it must not
    scale linearly with the unit count.

    No score in the ZSET = never reported = judged offline directly, without an
    extra Hash hit (the pre-seeded 100 units at init.sql start almost entirely in
    this state).
    """
    now = time.time() if now is None else now
    ids = [x.get("robot_id") for x in robots if x.get("robot_id")]
    scores: dict[str, float] = {}
    hash_fields: dict[str, dict[str, str]] = {}
    try:
        r = await get_redis()
        pipe = r.pipeline()
        for rid in ids:
            pipe.zscore(KEY_ZSET, rid)
        for rid in ids:
            pipe.hgetall(f"{KEY_PREFIX}{rid}")
        out = await pipe.execute()
        for rid, score in zip(ids, out[: len(ids)]):
            if score is not None:
                scores[rid] = float(score)
        for rid, h in zip(ids, out[len(ids):]):
            if h:
                hash_fields[rid] = h
    except Exception:
        log.exception("liveness enrich failed, falling back to all-offline")

    for item in robots:
        rid = item.get("robot_id")
        last_seen = scores.get(rid)
        h = hash_fields.get(rid, {})
        if last_seen is None:
            # Never reported: US-1's "first-screen offline card" semantics, kept.
            item["is_online"] = False
            item["last_seen"] = None
            continue
        item["is_online"] = (now - last_seen) <= settings.robot_offline_threshold_s
        item["last_seen"] = int(last_seen)
        # Last frame's status_code (for card coloring / temperature display). When
        # absent in the Hash, keep the snapshot's own value.
        sc = _to_int(h.get("status_code"))
        if sc is not None:
            item["status_code"] = sc
    return robots


async def online_count(*, now: float | None = None) -> int:
    """Current online count (members in the ZSET whose last_seen falls within the
    judgment window).

    `ZCOUNT`'s semantics is exactly "members with score in [min, max]", identical
    to per-robot `now - last_seen <= t`, but in only one round-trip -- no need to
    walk all 100 units just for the "online 37/100" number.
    """
    now = time.time() if now is None else now
    try:
        r = await get_redis()
        return int(await r.zcount(KEY_ZSET, now - settings.robot_offline_threshold_s, "+inf"))
    except Exception:
        log.exception("liveness online_count failed")
        return 0


def _to_float(v: str | None) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(v: str | None) -> int | None:
    f = _to_float(v)
    return None if f is None else int(f)
