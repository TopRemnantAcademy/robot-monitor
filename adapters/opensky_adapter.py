#!/usr/bin/env python3
"""OpenSky -> RobotMonitor adapter (public-data source PoC).

Motivation
----------
RobotMonitor's telemetry schema (robot_telemetry) was originally shaped for 100
humanoid robots. The schema was relaxed (see infra/init.sql / models.py:
battery_pct, max_joint_temp and the three JSONB joint columns are now NULLABLE)
so that heterogeneous public-data feeds can reuse the same hypertable and the
existing dashboard, without standing up a second pipeline.

This adapter is the reference implementation of that enablement: it pulls live
aircraft state vectors from the OpenSky Network REST API and maps each aircraft
to a telemetry-shaped row plus a robot_metadata row, writing them directly to
PostgreSQL/TimescaleDB (the same 17-column contract used by
backend_ingestion's batch_writer). The existing React dashboard then renders
the aircraft as "robots" with no frontend changes (battery/joint panels simply
read NULL).

Why direct DB writes (not MQTT -> backend_ingestion)
--------------------------------------------------
For a PoC this is the lowest-friction path: it needs only DATABASE_URL plus
network egress to OpenSky, and exercises the relaxed schema end to end. The
production shape would publish to the MQTT broker and let backend_ingestion
ingest normally; the mapping below is identical, only the sink differs.

Known PoC limitation
--------------------
The dashboard's fleet-online denominator is hard-coded to 100 (the seeded
humanoids). Aircraft written here raise the total robot count but are not yet
reflected in that 100-based ratio; that is a frontend follow-up, not a blocker
for showing the 108 aircraft on the map/cards.

OpenSky API notes
-----------------
- Anonymous endpoint: https://opensky-network.org/api/states/all
- Bounding-box query (lamin/lomin/lamax/lomax) keeps the result set focused.
- Anonymous rate limit is strict (credits/min + 10s floor); the default poll
  interval is 30s and --interval is floored at 15s to avoid HTTP 429.

Usage
-----
  # Live, default Korean-airspace bbox, loop forever:
  DATABASE_URL=postgresql://... python adapters/opensky_adapter.py

  # One poll, print mapped rows, do NOT write to the database:
  python adapters/opensky_adapter.py --dry-run

  # Offline smoke test: synthesize 108 aircraft and write them once (no network):
  DATABASE_URL=postgresql://... python adapters/opensky_adapter.py --demo --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("opensky_adapter")

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
DEFAULT_BBOX = (33.0, 124.0, 39.0, 132.0)  # (lamin, lomin, lamax, lomax) ~ Korean peninsula
MODEL_TYPE = "AIRCRAFT-OPENSKY"

# Column order is byte-for-byte the same 17 columns as
# backend_ingestion/app/services/batch_writer.py:COLUMNS so the two sinks stay
# interchangeable.
TELEMETRY_COLUMNS = (
    "time", "robot_id", "status_code", "error_code", "battery_pct", "battery_temp",
    "max_joint_temp", "joint_temperatures", "joint_positions", "joint_torques",
    "pos_x", "pos_y", "pos_z", "orient_w", "orient_x", "orient_y", "orient_z",
)

INSERT_TELEMETRY = f"""
INSERT INTO robot_telemetry ({", ".join(TELEMETRY_COLUMNS)})
VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11,$12,$13,$14,$15,$16,$17)
ON CONFLICT (time, robot_id) DO NOTHING
"""

UPSERT_METADATA = """
INSERT INTO robot_metadata (robot_id, model_type, is_online, last_seen)
VALUES ($1, $2, $3, $4)
ON CONFLICT (robot_id) DO UPDATE
  SET is_online = EXCLUDED.is_online,
      last_seen = EXCLUDED.last_seen,
      model_type = EXCLUDED.model_type
"""


def _monotonic_ts_ms(last_ms: dict, robot_id: str, now_ms: int) -> int:
    """Issue a strictly-increasing per-robot millisecond timestamp.

    robot_telemetry's primary key is (time, robot_id); two rows for the same
    aircraft in the same millisecond collide and are dropped by ON CONFLICT
    DO NOTHING. Mirror backend_ingestion's batch_writer._to_row rule.
    """
    prev = last_ms.get(robot_id)
    if prev is None or now_ms > prev:
        ts = now_ms
    else:
        ts = prev + 1
    last_ms[robot_id] = ts
    return ts


def _state_to_row(state: list, last_ms: dict) -> tuple:
    """Map one OpenSky state vector to a 17-element telemetry tuple.

    OpenSky state vector layout (relevant fields):
      0 icao24, 1 callsign, 5 longitude, 6 latitude, 7 baro_altitude,
      8 on_ground, 13 geo_altitude
    Aircraft have no battery/joints, so those columns are NULL (now allowed).
    """
    icao24 = state[0]
    robot_id = f"opensky-{icao24}"
    on_ground = bool(state[8])
    status_code = 0 if on_ground else 1
    lon = state[5]
    lat = state[6]
    alt = state[13] if state[13] is not None else state[7]  # geo_altitude, else baro
    now_ms = int(time.time() * 1000)
    ts = _monotonic_ts_ms(last_ms, robot_id, now_ms)
    dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    # orient_* stay NULL for aircraft; the dashboard 3D pose simply reads None.
    return (
        dt, robot_id, status_code, 0,
        None,  # battery_pct
        None,  # battery_temp
        None,  # max_joint_temp
        None,  # joint_temperatures
        None,  # joint_positions
        None,  # joint_torques
        lon, lat, alt,
        None, None, None, None,  # orient_w/x/y/z
    )


def _demo_states(count: int, bbox) -> list:
    """Synthesize `count` aircraft state vectors inside `bbox` (no network)."""
    lamin, lomin, lamax, lomax = bbox
    states = []
    for i in range(count):
        icao = f"{random.randint(0, 0xFFFFFF):06X}".lower()
        states.append([
            icao, f"DEMO{i:03d}", "KP", None, int(time.time()),
            random.uniform(lomin, lomax), random.uniform(lamin, lamax),
            random.uniform(1000, 11000), False, random.uniform(0, 250),
            random.uniform(0, 360), random.uniform(-10, 10), None,
            random.uniform(1000, 11000), None, False, 0,
        ])
    return states


async def fetch_states(bbox, http_timeout: float = 30.0) -> list:
    """GET OpenSky /states/all for the bounding box; return raw state vectors."""
    import httpx  # repo-standard HTTP client (no hand-rolled sockets)

    lamin, lomin, lamax, lomax = bbox
    params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        resp = await client.get(OPENSKY_STATES_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    return payload.get("states") or []


async def write_rows(pool, telemetry_rows: list, meta_rows: list) -> None:
    async with pool.acquire() as conn:
        if telemetry_rows:
            await conn.executemany(INSERT_TELEMETRY, telemetry_rows)
        if meta_rows:
            await conn.executemany(UPSERT_METADATA, meta_rows)


def build_rows(states: list, last_ms: dict):
    """Return (telemetry_rows, meta_rows) for a batch of state vectors."""
    tel = []
    meta = []
    now = datetime.now(timezone.utc)
    for st in states:
        row = _state_to_row(st, last_ms)
        robot_id = row[1]
        tel.append(row)
        on_ground = bool(st[8])
        meta.append((robot_id, MODEL_TYPE, not on_ground, now))
    return tel, meta


async def run_once(args, pool, last_ms, http_timeout) -> int:
    if args.demo:
        states = _demo_states(args.limit or 108, args.bbox)
    else:
        states = await fetch_states(args.bbox, http_timeout)
    if args.limit:
        states = states[: args.limit]
    tel, meta = build_rows(states, last_ms)
    if args.dry_run:
        for r in tel[:5]:
            log.info("sample row: robot_id=%s status=%s lon=%s lat=%s alt=%s",
                     r[1], r[2], r[10], r[11], r[12])
        log.info("[dry-run] %d aircraft mapped, NOT written", len(tel))
        return len(tel)
    if pool is None:
        log.error("no DATABASE_URL configured; cannot write (use --dry-run)")
        return 0
    await write_rows(pool, tel, meta)
    log.info("wrote %d telemetry rows + %d metadata rows", len(tel), len(meta))
    return len(tel)


async def amain(args) -> int:
    last_ms = {}
    pool = None
    if not args.dry_run:
        dsn = args.db_url or os.environ.get("DATABASE_URL")
        if not dsn:
            log.error("DATABASE_URL not set and not --dry-run; aborting")
            return 2
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        if args.once:
            return await run_once(args, pool, last_ms, args.http_timeout)
        polls = 0
        while True:
            await run_once(args, pool, last_ms, args.http_timeout)
            polls += 1
            if args.polls and polls >= args.polls:
                break
            await asyncio.sleep(args.interval)
    finally:
        if pool:
            await pool.close()
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OpenSky -> RobotMonitor adapter (public-data PoC)")
    p.add_argument("--db-url", default=None, help="PostgreSQL DSN (else $DATABASE_URL)")
    p.add_argument("--bbox", nargs=4, type=float, default=DEFAULT_BBOX,
                   metavar=("LAMIN", "LOMIN", "LAMAX", "LOMAX"),
                   help="bounding box (default: Korean peninsula)")
    p.add_argument("--interval", type=float, default=30.0,
                   help="poll interval seconds (floored to 15s to respect OpenSky limits)")
    p.add_argument("--limit", type=int, default=None,
                   help="max aircraft per poll (default: all in bbox)")
    p.add_argument("--once", action="store_true", help="single poll then exit")
    p.add_argument("--polls", type=int, default=None, help="stop after N polls")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch/map but do NOT write to the database")
    p.add_argument("--demo", action="store_true",
                   help="synthesize aircraft (no network) for offline testing")
    p.add_argument("--http-timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    if args.interval < 15.0 and not args.once:
        args.interval = 15.0  # respect OpenSky anonymous rate limits
    return args


def main() -> int:
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
