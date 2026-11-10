"""FastAPI entry point (spec/01 §4.5 + spec/00 §5.2/§5.3).

Outbound contract (any missing piece is a blocker):
- WS  `/ws/robots`   —— outbound MUST be a type-tagged envelope: snapshot / telemetry / alert
                       and MUST subscribe to Redis `robot:alerts` to forward alerts
                       (otherwise the frontend alert stream is permanently empty)
- REST `GET /robots`                     —— latest status of 100 robots (fallback outside WS)
- REST `GET /robots/{id}/history`        —— data source for the Drawer's ECharts lines + joint heatmap
- REST `GET /alerts`                     —— alert pagination (Order 17, readable from viewer role)
- REST `POST /alerts/{id}/ack|unack`     —— alert ack/unack (Order 17, requires admin)
- REST `GET /healthz`                    —— health check
- REST `GET /metrics`                    —— Prometheus scrape (**must be unauthenticated**)

Auth boundary (Order 15):
- Only two endpoints are unauthenticated: `/healthz` (liveness probe) and `/metrics`
  (Prometheus **pull**). Adding auth to the probe would crash container orchestration;
  adding auth to `/metrics` is equivalent to silently disabling monitoring.
- All other REST uses `Authorization: Bearer <JWT>` (**does not accept** token in query,
  otherwise the token lands in nginx/gateway access logs);
- `/ws/robots` uses `?token=` (browser WebSocket API cannot set custom headers, only query).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from app import metrics, telemetry_tracing
from app.alerts.router import router as alerts_router
from app.auth.deps import authenticate_ws_token, current_active_user
from app.auth.router import router as auth_router
from app.auth.users import RobotUser, close_engine
from app.config import settings
from app.db.redis import close_redis
from app.db.session import close_pool, ping
from app.logging_json import configure_json_logging
from app.schemas.api import (
    HealthResponse,
    RobotHistoryResponse,
    RobotListResponse,
    SupportedRobotsResponse,
)
from app.catalog import lookup
from app.security_middleware import install_cors, install_rate_limit
from app.services import liveness, mqtt_subscriber, state_cache
from app.services.batch_writer import BatchWriter
from app.services.metadata_writer import metadata_writer
from app.services.offline_buffer import DurableBuffer, run_replay
from app import ws_probe
from app.ws_backpressure import WsOutbound
from app.ws_hub import hub as ws_hub

# Order 16 T4: single-line JSON logs, for Order 07's openobserve/loki to scrape directly.
# Must be configured BEFORE any other logger's first output — uvicorn's startup log goes
# through the root logger; configuring late means the first few lines are still text and
# the scraper sees a mixed format.
configure_json_logging(logging.INFO)
log = logging.getLogger("main")

writer = BatchWriter()

# R-12: span routing and upper bound for /history (history() specific; see infra/init.sql §5.1b).
# Size threshold: > 3h goes to the hourly bucket. 180 is used instead of 1440 (24h) because
# 3h × 10s/bucket ≈ 1080 points/robot is already near the readable ceiling for the frontend
# line; beyond that you must downsample — better to switch early than late.
HOURILY_AFTER_MINUTES = 180
# Upper bound 7 days = 10080 minutes. This is a GUARDRAIL, not precise capacity planning:
# `minutes` had no upper bound before; a single `?minutes=525600` (one year) is enough to do a
# full-table scan on the raw table and exhaust the connection pool. 7d leaves headroom against
# the 30-day raw retention (§6.2) and also covers the real ops need of "one-week trend".
MAX_HISTORY_MINUTES = 7 * 24 * 60  # 10080


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    writer_task = asyncio.create_task(writer.run())
    # robot_metadata heartbeat (G-6 / ISSUE #96): a side path independent of batch_writer,
    # collapsed at 1s + single-statement batched UPDATE. Its daemon style matches writer's —
    # both are "start and stay resident, explicitly flush before shutdown". When the switch is
    # off, run() returns immediately and the task self-terminates.
    metadata_task = asyncio.create_task(metadata_writer.run())
    mqtt_task = asyncio.create_task(mqtt_subscriber.run(writer))
    # WS synthetic probe: client-perspective liveness check (gap G-3). It only reads metrics,
    # never touches the data stream; when off, run() returns immediately and the task
    # self-terminates — no special shutdown handling needed.
    probe_task = asyncio.create_task(ws_probe.run())
    # Offline buffer replay loop (Benchmark C: store-and-forward). Re-injects rows buffered
    # during a DB outage once the DB recovers. The Redis connection is lazy (no connect until
    # actually used); buffering/replay are both failure-safe (no normal data loss). No task is
    # created when disabled.
    offline_task = None
    if settings.offline_buffer_enabled:
        from app.db.redis import get_redis

        _buffer = DurableBuffer(
            await get_redis(), settings.offline_buffer_key, settings.offline_buffer_maxlen
        )
        offline_task = asyncio.create_task(
            run_replay(writer, _buffer, settings.offline_buffer_replay_interval_s)
        )
    # WS outbound hub (ISSUE #71): the only process-wide Redis pubsub subscription + 100ms
    # merge tick. Must start() before the first WS connection, otherwise early connections
    # miss incremental frames.
    await ws_hub.start()
    app.state.writer_task = writer_task
    app.state.metadata_task = metadata_task
    app.state.mqtt_task = mqtt_task
    app.state.probe_task = probe_task
    app.state.offline_task = offline_task
    # Order 16 T3: endpoint is a no-op if empty, returning False does not affect startup
    # (can degrade gracefully). Placed before yield: instrumentation must precede the first
    # request arriving.
    tracing_on = telemetry_tracing.configure_tracing(
        settings.otel_exporter_otlp_endpoint, settings.otel_service_name
    )
    if tracing_on:
        telemetry_tracing.instrument_fastapi(app)
    log.info(
        "backend started: mqtt=%s:%d tracing=%s",
        settings.mqtt_broker,
        settings.mqtt_port,
        "on" if tracing_on else "off",
    )
    yield
    # shutdown —— order must not be swapped; the official pattern is at
    # external/aiomqtt/docs/alongside-fastapi-and-co.md @ v2.5.1
    #   ① Stop MQTT first, cut off the inlet, stop producing new telemetry
    mqtt_task.cancel()
    await asyncio.gather(mqtt_task, return_exceptions=True)
    #   ② Flush writer's backlog: graceful_shutdown() posts a sentinel to wake run(), then
    #      await writer_task to let it finish flushing this batch before returning. If
    #      writer_task were cancelled before this, run() would be interrupted mid-_flush and
    #      the last batch of telemetry (the ~30000 rows Order 06 acceptance cares about) would
    #      be lost.
    await writer.graceful_shutdown()
    await asyncio.gather(writer_task, return_exceptions=True)
    #   ③ Then flush the metadata heartbeat backlog. MUST be before close_pool(): stop() only
    #      sets the flag and wakes run(); the real flush() waits for run() to finish, so the
    #      pool must still be alive then.
    await metadata_writer.stop()
    await asyncio.gather(metadata_task, return_exceptions=True)
    #   ④ The probe is only a bystander with no backlog to flush — cancel directly (it sleeps
    #      on the interval).
    probe_task.cancel()
    await asyncio.gather(probe_task, return_exceptions=True)
    #   ④b Cancel the offline buffer replay loop (buffering/replay are failure-safe, so an
    #      immediate cancel is fine).
    if offline_task is not None:
        offline_task.cancel()
        await asyncio.gather(offline_task, return_exceptions=True)
    #   ⑤ WS hub: stop fan-out and the shared subscription first, then clear the registry —
    #      MUST be before close_redis, otherwise _consume_loop would backoff-reconnect against
    #      an already-closed Redis and spam the logs.
    await ws_hub.stop()
    await close_pool()
    await close_redis()
    # auth's ORM engine is independent of the asyncpg business pool (fastapi-users needs a
    # SQLAlchemy session), so it must also be released at teardown, otherwise connections
    # hang until the server-side timeout when the container exits.
    await close_engine()
    # Finally tear down tracing: the BatchSpanProcessor queue may still hold spans from the
    # shutdown phase.
    telemetry_tracing.shutdown_tracing()
    log.info("backend stopped")


app = FastAPI(title="RobotMonitor Ingestion", lifespan=lifespan)

# R-17: CORS + optional rate limiting (middleware mount; does NOT change any route handler/
# contract/auth). Must be mounted before the first request arrives — Starlette's
# add_middleware only works before app startup. Both default to "off/minimal exposure", so
# behavior is byte-for-byte identical to before when env is unconfigured:
#   - CORS_ALLOW_ORIGINS empty   → no CORS middleware (same-origin deploy is enough)
#   - RATE_LIMIT_ENABLED=false   → no rate-limit middleware
# Mount order has no mutual dependency (one handles response headers, the other request
# counts), but both are before include_router, so they apply to ALL routes (incl. /auth/*
# login endpoints).
install_cors(app, settings.cors_allow_origins)
install_rate_limit(
    app,
    enabled=settings.rate_limit_enabled,
    per_minute=settings.rate_limit_per_minute,
)

# Auth routes (/auth/register, /auth/jwt/login, /auth/jwt/logout, /users/me).
# They add NO auth dependency — a login endpoint requiring its own token would be a dead loop.
app.include_router(auth_router)

# Alert closed-loop (Order 17) — GET /alerts pagination + POST /alerts/{id}/ack|unack.
# Mounted under the same app with absolute paths (/alerts...), no prefix.
app.include_router(alerts_router)


# ------------------------------------------------------------------ REST

@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Health check.

    `status` reflects DEPENDENCY reachability, not just "process alive":
    - `ok`       —— DB and Redis both reachable
    - `degraded` —— process alive but deps unreachable (writes queue up; alert now)
    - still returns HTTP 200: this is liveness-probe semantics; switching to 503 would let
      the container be restarted, yet the service self-heals once deps recover, and restart
      would lose in-flight data. Unhealthy deps are expressed via the `status` field.
    """
    db_ok = await _db_healthy()
    # Online count (ISSUE #77) is refreshed here as a bonus: /healthz is exactly the probe the
    # orchestration system hits at second-level frequency, so piggybacking on its rhythm to
    # maintain the Gauge avoids a separate timer for Prometheus (in a pull model, a Gauge
    # nobody sets stays at its initial value forever — the source of the "constant 0" false
    # green).
    online = await liveness.online_count()
    metrics.robots_online.set(online)
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "up" if db_ok else "down",
        "written": writer.written,
        "flushes": writer.flushes,
        "retries": writer.retries,
        "rejected": writer.rejected,
        "buffered": writer.buffered,
        "queue_depth": writer.queue.qsize(),
        "mqtt_received": mqtt_subscriber.received,
        "mqtt_dropped": mqtt_subscriber.dropped,
        # Row-loss triplet (ISSUE #19 G2): submitted is rows handed to the store, written is
        # rows actually stored, skipped is rows silently dropped by ON CONFLICT DO NOTHING.
        # Together with rejected (bad rows judged unwritable by the degrade chain) they are the
        # complete answer to "how many received / how many stored".
        "rows_submitted": writer.rows_submitted,
        "rows_written": writer.written,
        "rows_skipped": writer.rows_skipped,
        # robot_metadata heartbeat (G-6 / ISSUE #96): flushes is the number of refresh rounds,
        # rows_written is the number of robots hit by UPDATE, deduped is the duplicate rows
        # collapsed — at 100 robots × 10Hz steady state deduped/flushes ≈ 900, a direct reading
        # that "batch semantics actually took effect".
        "metadata": metadata_writer.stats,
    }


# Current active WS connection count (Order 16 T1b's `robot_backend_ws_connections`,
# renamed in ISSUE #72). An in-process integer rather than writing the Gauge directly:
# Gauge.set is refreshed uniformly in /metrics (pull model); the "fact" maintained here must
# update exactly when a connection is truly established/reclaimed — the two moments must not
# be mixed. try/finally ensures the exception path also reclaims, otherwise the count only
# ever grows (false alarm).
_ws_active = 0


_db_probe: tuple[float, bool] | None = None


async def _db_healthy() -> bool:
    """DB liveness probe with caching.

    Purpose of caching: the probe is called at second-level frequency by orchestration;
    hitting the DB every time wastes connections and amplifies failures. Within the cache
    window (settings.db_health_cache_s) the last result is reused, lowering the probe
    frequency to the window frequency during failures.
    """
    global _db_probe
    now = asyncio.get_running_loop().time()
    if _db_probe is not None and now - _db_probe[0] < settings.db_health_cache_s:
        return _db_probe[1]
    ok = await ping()
    _db_probe = (now, ok)
    return ok


@app.get(
    "/metrics",
    # Explicitly declare text/plain: Prometheus exposition format is not JSON. Without this,
    # FastAPI defaults to inserting `application/json` + an empty schema, so the contract in
    # the snapshot is written as JSON — contradicting the real response body, exactly the
    # "documentation vs contract drift" this order aims to eliminate. Using
    # response_class=PlainTextResponse REPLACES that default content-type (writing responses=
    # {...} only appends a JSON entry that would still remain).
    response_class=PlainTextResponse,
)
async def prometheus_metrics():
    """Prometheus scrape endpoint (the `robot_backend` job in infra/observability/prometheus.yml).

    The difference from /healthz is the MODEL, not the content: /healthz returns an instant
    snapshot, so the scraper always gets "the values at the last write moment"; here the
    Counter is monotonically accumulating, so Prometheus can compute rate() / increase(),
    giving the 1000 msg/s a historical curve.

    The function name must NOT be `metrics` — that would shadow the module name `app.metrics`
    (from app import metrics).
    """
    # Instant values (Gauge) must be refreshed before exposure: in a pull model nobody sets
    # them periodically for us; without refresh queue_depth stays at the last write moment and
    # the scrape always gets stale values. Order 16 acceptance requires real queue-water-level
    # fluctuation under load testing (not constant 0), which relies on exactly this set —
    # without it, the Gauge initial value is 0 and never changes. Only this set remains after
    # ISSUE #72 (no duplicate refresh).
    metrics.refresh_gauges(writer.queue.qsize(), _ws_active)
    body, ctype = metrics.render()
    # ctype is given by prometheus_client (`text/plain; version=0.0.4; charset=utf-8`) and must
    # be passed through verbatim — the version number is how the Prometheus parser negotiates
    # the format and cannot be dropped.
    return PlainTextResponse(content=body, media_type=ctype)


@app.get("/robots", response_model=RobotListResponse)
async def list_robots(_user: RobotUser = Depends(current_active_user)):
    """Latest status of 100 robots. Read-only endpoint: accessible from viewer role up; the
    response body is byte-for-byte identical to before auth was added.

    ISSUE #77: the response adds two optional fields `is_online` / `last_seen`, and ALWAYS
    returns 100 robots (a robot that stopped reporting falls back to an offline placeholder
    rather than disappearing from the list).
    """
    return {"robots": await state_cache.snapshot(settings.robot_ids)}


@app.get("/supported-robots", response_model=SupportedRobotsResponse)
async def supported_robots(_user: RobotUser = Depends(current_active_user)):
    """Catalog of platform-supported models + composition of the registered fleet.

    The source of truth for "which models the platform supports" is the distinct
    robot_metadata.model_type (DB-driven, zero hallucination); display metadata (name/
    description/DOF/form) is supplemented by app.catalog.lookup(); unregistered models fall
    back to a placeholder, never fabricated. Readable from viewer role up (this is a public
    description of "platform capability", with no write operations).

    The response also gives each model's registered total / online count / firmware versions /
    station distribution — the UI uses it to render "supported models" cards; the admin UI
    additionally shows fleet-management details.
    """
    from app.db.session import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT model_type,
                   COUNT(*)                                                   AS total,
                   COUNT(*) FILTER (WHERE is_online)                        AS online,
                   array_remove(
                       array_agg(DISTINCT firmware_version)
                       FILTER (WHERE firmware_version IS NOT NULL), NULL
                   )                                                         AS firmwares,
                   COUNT(DISTINCT assigned_station)
                       FILTER (WHERE assigned_station IS NOT NULL)           AS stations
            FROM robot_metadata
            GROUP BY model_type
            ORDER BY total DESC, model_type
            """
        )

    models = []
    total_robots = 0
    for r in rows:
        meta = lookup(r["model_type"])
        total_robots += r["total"]
        models.append(
            SupportedRobotOut(
                model_type=r["model_type"],
                display_name=meta.display_name,
                form_factor=meta.form_factor,
                dof=meta.dof,
                vendor=meta.vendor,
                description=meta.description,
                generic=meta.generic,
                total=r["total"],
                online=r["online"],
                firmware_versions=list(r["firmwares"] or []),
                stations=r["stations"],
            )
        )
    return {"models": models, "total_robots": total_robots}


@app.get("/robots/{robot_id}/history", response_model=RobotHistoryResponse)
async def history(
    robot_id: str,
    minutes: int = 5,
    resolution: str = "10s",
    _user: RobotUser = Depends(current_active_user),
):
    """History for the last N minutes (for the Drawer lines + joint heatmap).

    Data source (since ISSUE #76): PREFER the continuous aggregate `robot_history_10s`, whose
    refresh policy is end_offset=10s / schedule=30s, with latency within tens of seconds (see
    infra/init.sql §5.1), satisfying the Drawer's need for "recent data".

    Why no longer querying the raw table directly:
      The old implementation did time_bucket on robot_telemetry, filtering time >= now() - 5min.
      100 robots × 10Hz = 1000 rows/s ⇒ ~3000 rows per robot in a 5-min window, far more for a
      full scan; every Drawer open required an interval scan on the raw hypertable + per-row
      last() aggregation. Switching to the continuous aggregate reads only "~30 pre-aggregated
      rows/robot" for the same query, cutting scan volume by three orders of magnitude — this is
      real speed, not false speed observed via a Histogram.

    Why keep the raw-table fallback:
      The continuous aggregate is empty before REFRESH (WITH NO DATA + async refresh policy), so
      a cold start, an unrun policy job, or a refresh window not yet covered makes the direct
      query return an empty array → the Drawer line is permanently empty. The fallback guarantees
      "aggregate not ready ≠ endpoint broken", semantically identical to spec/00 §5.3 "fall back
      to raw table when aggregate not ready".

    `last(joint_positions, time)` MUST be kept in BOTH branches — it is the ONLY data source for
    the Drawer's third line "angle (rad)" (RobotDetailDrawer.tsx:88); without it the line is
    permanently null (a pitfall from Order 16).

    Span routing (R-12): `minutes <= HOURILY_AFTER_MINUTES(180)` still uses the 10s aggregate,
    byte-for-byte consistent with ISSUE #76 (R-03 §4 contract hard red line); beyond that it
    switches to the hourly aggregate `robot_history_1h` (infra/init.sql §5.1b), compressing a
    24h window from 8640 points/robot to 24 points/robot. The hourly path's `resolution` echoes
    `3600s` (the real bucket seconds, not the input `10s`) — `resolution` is the fact label of
    "how many seconds each point represents"; echoing the input would make the frontend treat a
    1h bucket as a 10s bucket. Do NOT introduce `resolution=1h`: `_parse_resolution` is locked
    to integer-second semantics by R-03 §4.1; hourly downsampling is triggered by the `minutes`
    threshold, orthogonal to the `resolution` parameter.
    """
    try:
        res_s = _parse_resolution(resolution)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid resolution: {resolution}")

    # Upper-bound guard (R-12 T2): must reject BEFORE acquiring a connection, otherwise a huge
    # span query has already thrown a full-table scan at the DB before returning 400 — the
    # guard loses its meaning. Kept outside `_parse_resolution`: that validates `resolution`
    # (R-03 §4.1); this validates `minutes`; different responsibilities, mixing them would
    # shake R-03's locked surface.
    if minutes > MAX_HISTORY_MINUTES:
        raise HTTPException(status_code=400, detail="history span too large")

    span_s = max(1, minutes) * 60
    # Span routing (R-12):
    #   small span → 10s aggregate (`sql_cagg`, byte-for-byte consistent with ISSUE #76, R-03
    #   regression red line);
    #   large span → hourly aggregate (`sql_cagg` swapped to robot_history_1h, rest of template
    #   unchanged).
    # Only `source` changes; the bucket expression still reads the materialized column `bucket`
    # — the 1h view's materialized column is also named bucket, isomorphic to the 10s view, so
    # no need to wrap another time_bucket by bucket width (which would hide the auto-built index).
    hourly = minutes > HOURILY_AFTER_MINUTES
    agg_source = "robot_history_1h" if hourly else "robot_history_10s"
    # Echo the ACTUAL bucket seconds in the response: 3600 for the hourly path, the input
    # res_s for the small path.
    out_res_s = 3600 if hourly else res_s

    # The SELECT columns of both branches are byte-for-byte aligned (column name = response
    # field name), so the result mapping is written once and there is no drift of "aggregate
    # added a column, fallback missed it". The only difference is FROM / WHERE: the aggregate
    # table filters by `bucket`, the raw table by `time`.
    _select = """
        SELECT {bucket}                          AS bucket,
               robot_id,
               last(status_code, time)           AS status_code,
               last(battery_pct, time)           AS battery_pct,
               max(max_joint_temp)               AS max_joint_temp,
               last(joint_temperatures, time)    AS joint_temperatures,
               last(joint_torques, time)         AS joint_torques,
               -- joint_positions must be returned: the Drawer's third line "angle (rad)" data
               -- source is point.joint_positions[joint] (RobotDetailDrawer.tsx:88).
               -- When missing, that line is permanently null, the graph permanently blank.
               last(joint_positions, time)       AS joint_positions
        FROM {source}
        WHERE robot_id = $2
          AND {time_col} >= now() - (($3 || ' seconds')::interval)
        GROUP BY bucket, robot_id
        ORDER BY bucket
    """
    sql_cagg = _select.format(
        bucket="bucket", time_col="bucket", source=agg_source
    )
    sql_raw = _select.format(
        bucket="time_bucket(($1 || ' seconds')::interval, time)",
        time_col="time",
        source="robot_telemetry",
    )

    from app.db.session import get_pool

    pool = await get_pool()
    # Order 16 T1b: /history latency histogram. Timing covers ONLY the DB query (including
    # acquire), NOT parameter validation and result serialization — the former is a database
    # problem, the latter a CPU problem; mixing them in one bucket would deprive the "should we
    # build the continuous-aggregate table" decision of its basis (serialization time does not
    # grow linearly with data volume, query time does). try/finally rather than only observing
    # on success: the moment a query fails is often the slowest one (timeout/lock wait); missing
    # it would systematically underestimate p99.
    _t0 = time.perf_counter()
    # source is counted only AFTER the query returns successfully, so its initial value is None
    # rather than "cagg" — if the exception path also counted it as cagg, it would mix "aggregate
    # query failed" into the numerator of "aggregate hit", precisely masking the most exposed
    # failure as the most ideal state.
    source: str | None = None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql_cagg, str(res_s), robot_id, str(span_s))
            if not rows:
                # Aggregate not ready (policy hasn't refreshed to this window) → fall back to raw
                # table. Fall back ONLY on empty results, not unconditionally double-querying:
                # the non-empty path shouldn't pay a second cost, otherwise the gain from
                # switching to continuous aggregates would be cancelled out by itself.
                source = "raw"
                rows = await conn.fetch(sql_raw, str(res_s), robot_id, str(span_s))
            else:
                source = "cagg"
    except Exception as exc:
        log.exception("history query failed")
        return JSONResponse(status_code=503, content={"detail": f"query failed: {exc}"})
    finally:
        metrics.history_latency_seconds.observe(time.perf_counter() - _t0)
        # Data-source label: whether the p95 drop is "truly faster" or "queried a different
        # table" is distinguished by this. On the exception path source is still None, not
        # counted in any series (failure is exposed by means other than `m`: 503 + log.exception),
        # avoiding disguising failure as a hit.
        if source is not None:
            metrics.history_source.labels(source=source).inc()

    # Note: robot_metadata is deliberately NOT validated here. init.sql preseeds 100 robots, but
    # a robot that "never reported data" querying history is necessarily an empty array — that is
    # normal semantics, not an error. Changing it to 404 would make the Drawer pop an error for a
    # just-online robot, misguiding operations.
    #
    # Likewise (ISSUE #77): you CANNOT infer offline from "history is empty" — a robot that
    # stopped reporting still has lines in its history; it just "has no new points recently".
    # The sole source of truth for online state is /robots and the WS snapshot's `is_online`;
    # this endpoint does not participate in the judgment.

    return {
        "robot_id": robot_id,
        "resolution": f"{out_res_s}s",
        "points": [
            {
                "time": r["bucket"].isoformat(),
                "status_code": r["status_code"],
                "battery_pct": r["battery_pct"],
                "max_joint_temp": r["max_joint_temp"],
                "joint_temperatures": _as_dict(r["joint_temperatures"]),
                "joint_torques": _as_dict(r["joint_torques"]),
                "joint_positions": _as_dict(r["joint_positions"]),
            }
            for r in rows
        ],
    }


def _parse_resolution(resolution: str) -> int:
    """'10s' / '30s' / '1m' → integer seconds. Non-integer multiples ('2.5s' / '1.5m') are
    REJECTED, not silently truncated.

    Issue #74: the old implementation did `int(float(...))` directly, silently turning '2.5s'
    into 2 seconds, '1.5m' into 90 seconds, '0.5m' into 30 seconds — the resolution the user
    wrote differed from the actual query resolution, with NO error hint. This "almost right"
    silent degradation is the hardest to debug on an ops dashboard: the number of points on the
    graph doesn't match expectations, yet HTTP 200, no alarm.

    So this changes to: accept ONLY when the value is exactly an integer, otherwise raise
    ValueError, turned into HTTP 400 + `invalid resolution: <original>` by history()
    (main.py:227-229). Legal integer values ('10s'→10, '30s'→30, '1m'→60) behave byte-for-byte
    identically; the lower bound remains 1s (sub-1s still forbidden, to avoid crushing queries).

    [Why not multiply 'm' by 60 first then judge integer]
    '0.02m' × 60 = 1.2 seconds, which is >1 but not a whole second — meaningless for a
    time_bucket('1.2 seconds') non-integer-second bucket; judging "the minute count itself is an
    integer" is clearer, and also avoids fuzzy inputs like '1.6666666666666667m' ≈ 100s being
    let through because they "happen to be near an integer".
    """
    s = resolution.strip().lower()
    if s.endswith("ms"):
        raise ValueError("resolution below 1s not supported")
    unit = s[-1:] if s[-1:] in ("s", "m") else ""
    if not unit:
        raise ValueError(resolution)

    raw = s[:-1]
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(resolution) from None

    # Reject nan / inf: float('nan') is not caught by the except above (it parses successfully),
    # and nan != int(nan) is always true, int(nan) directly raises ValueError — block explicitly
    # so the error message stays stable as "invalid resolution" instead of a random math domain
    # error.
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(resolution)

    # Non-integer multiples (incl. '2.5s' / '1.5m' / '0.5m') → 400, no more silent truncation.
    if value != int(value):
        raise ValueError(f"resolution must be a whole number of seconds: {resolution}")

    seconds = int(value) * (60 if unit == "m" else 1)
    # Lower bound 1s (forbidden item: do not relax). '0.5m'=30 is legal, but '0m'/'0s' still
    # rejected.
    if seconds < 1:
        raise ValueError("resolution below 1s not supported")
    return seconds


def _as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {}
    return {}


async def _ws_watchdog(ws: WebSocket, outbound: WsOutbound) -> None:
    """Active eviction of slow clients / half-open connections (ISSUE #73).

    Two death criteria, either hit closes:

    1. Outbound queue persistently full: the client consumes slower than the 1000 msg/s inflow
       over the long term. Occasional full is normal (network jitter absorbed by the queue);
       persistently full means "this connection can't keep up" — keeping it hanging only makes
       the queue repeatedly drop frames and spam logs.
    2. Send persistently blocked: last_sent_at not advancing for a long time = the send coroutine
       stuck on send_json = TCP write buffer full but kernel hasn't errored (the classic
       "client unplugged network" half-open connection). TCP itself may take minutes to discover
       this state, and _ws_active would stay falsely high until then.

    Why a separate watchdog instead of reusing the heartbeat: the heartbeat only proves "the
    queue can still be filled"; it does NOT read whether the client is still consuming (server
    can send ≠ client can receive). Together they cover both "slow client" and "disconnected
    client" forms in the ISSUE #73 acceptance.
    """
    timeout = settings.ws_outbound_close_timeout_s
    # Use the event loop monotonic clock: the wall clock can jump on NTP, and the timeout
    # judgment must not be affected by it.
    loop = asyncio.get_running_loop()
    full_since: float | None = None
    last_sent_marker = 0

    while True:
        await asyncio.sleep(settings.ws_heartbeat_interval_s)
        now = loop.time()

        # Heartbeat sentinel: give the send coroutine a chance to verify liveness even when there
        # is no visible telemetry.
        outbound.heartbeat()
        metrics.outbound_queue_depth.set(outbound.qsize)

        # Criterion 1: queue persistently full
        if outbound.is_saturated:
            if full_since is None:
                full_since = now
            elif now - full_since >= timeout:
                log.warning(
                    "ws slow client evicted: outbound queue full for %.0fs", now - full_since
                )
                metrics.ws_slow_client_evicted.inc()
                await _ws_try_close(ws, code=1001, reason="slow consumer")
                return
        else:
            full_since = None

        # Criterion 2: send coroutine hasn't advanced for a long time
        if outbound.sent_count == last_sent_marker:
            if outbound.last_sent_at is not None and now - outbound.last_sent_at >= timeout:
                log.warning("ws half-open client evicted: no successful send for %.0fs", timeout)
                metrics.ws_slow_client_evicted.inc()
                await _ws_try_close(ws, code=1001, reason="heartbeat timeout")
                return
        else:
            last_sent_marker = outbound.sent_count


async def _ws_watch_disconnect(ws: WebSocket) -> None:
    """Wait until the client truly disconnects, then return (for the main loop's asyncio.wait race).

    [Why must read WebSocket explicitly]
    Before the change this endpoint was only OUTBOUND (await pubsub.listen → send_json) and never
    called receive. So when the client left, the server didn't know: TCP half-open means the
    kernel doesn't error, and _ws_active would stay falsely high, contradicting Order 16's
    anti-"false green" intent (ISSUE #73 root cause #2). Here `await ws.receive()` lets Starlette
    translate FIN/RST or the client close frame into an exception/return, so it's sensed
    immediately.

    [Why not reply pong like ws_probe]
    Starlette natively handles "ping" at the receive() layer: the client sends a text `ping`, and
    it auto-replies `pong` (the receive_text branch of starlette/websockets.py). This function
    only needs to consume messages, not parse content — parsing is the application layer's own
    business, and the contract says the client does not send business upstream frames (WS is a
    unidirectional downstream stream, see spec/00 §5.2).
    """
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _ws_try_close(ws: WebSocket, *, code: int, reason: str) -> None:
    """Best-effort close. Silent when already disconnected — the close action and the finally
    reclamation are two independent paths; failure here doesn't mean reclamation fails (don't
    re-raise, to avoid turning the watchdog into an unhandled task exception)."""
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        log.debug("ws close during eviction failed (already gone)", exc_info=True)


# ------------------------------------------------------------------ WebSocket

@app.websocket("/ws/robots")
async def ws_robots(ws: WebSocket, token: str | None = Query(default=None)):
    """Real-time stream. Auth via query parameter `?token=<JWT>`.

    Why query and not header: the browser `WebSocket` constructor CANNOT set custom request
    headers (new WebSocket(url, protocols) has no headers slot), so credentials must go in the
    URL. The cost is that the token lands in the server access log — a standard tradeoff, hence
    the REST side FORBIDS this (see Order 15 forbidden item 10); only WS opens this hole.

    Rejection order (a pitfall was stepped on here, recorded to avoid regression):
    The intuitive write is "validate first, close on failure", but under ASGI semantics a
    `websocket.close` sent BEFORE accept is translated by the protocol layer into an HTTP 403
    rejected handshake — the client gets `InvalidStatusCode: HTTP 403`, not the close code 1008
    required by the order (verified locally with websockets 12.0 client: `CLOSED:1008` is
    unreachable, only HTTP 403). So the order MUST be: accept() first, then immediately
    close(1008), sending no business frame in between. This satisfies both "close with 1008" and
    "must not send snapshot first" — they are not contradictory; what's contradictory is "close
    before accept".
    """
    sub = await authenticate_ws_token(token)
    await ws.accept()
    if sub is None:
        # 1008 = policy violation (RFC 6455 §7.4.1); the frontend distinguishes "rejected" from
        # "network down" by this. ⚠️ Between this line and the next statement, FORBID inserting
        # any send_json / send_text: that is the only window where an unauthorized party gets the
        # snapshot.
        await ws.close(code=1008)
        log.warning("ws rejected: missing or invalid token")
        return

    # Order 16 T1b: the connection count is +1 only AFTER auth passes. If placed after accept but
    # before auth, handshakes rejected with 1008 would also be counted, so a "1008 storm" (the
    # frontend reconnecting repeatedly with an expired token) would show as a falsely high
    # connection count, masking the real signal of "how many big screens are actually open".
    global _ws_active
    _ws_active += 1
    log.info("ws connected", extra={"ws_active": _ws_active})

    # ① First frame: full snapshot of 100 robots
    try:
        snap = await state_cache.snapshot(settings.robot_ids)
        await ws.send_json({"type": "snapshot", "data": {"robots": snap}})
    except Exception:
        log.exception("snapshot failed")

    # ② Incremental: telemetry + alerts (the alert channel is the only data source for the
    #    frontend alert stream)
    #
    # ISSUE #71 + #73 fused architecture: this connection creates its OWN bounded outbound
    # channel (#73), then registers with the process-level WS hub (#71) — the hub holds the only
    # process-wide Redis subscription and enqueues merged frames into each connection's queue on
    # a 100ms tick. This coroutine from here does only two things: register with the hub + wait
    # for disconnect. Before the change this was "each connection subscribes to
    # chan_stream_all/chan_alerts + sends each frame as send_json" — N big screens = N Redis
    # subscriptions + N × 1000 msg/s serialization/sending, linear blowup.
    outbound = WsOutbound(
        ws,
        maxsize=settings.ws_outbound_queue_size,
        alert_slack=settings.ws_alert_overflow_slack,
    )
    outbound.start()

    # Disconnect detection (ISSUE #73: can't judge a connection alive only by "can send"). The
    # hub fan-out only enqueues, never reads the WebSocket; a dedicated task `await ws.receive()`
    # is required: the client closing normally, crashing, or unplugging the network (RST/FIN)
    # makes receive raise WebSocketDisconnect, entering finally immediately.
    disconnect = asyncio.create_task(_ws_watch_disconnect(ws))

    # Backpressure / heartbeat timeout guard: evict actively when the queue is PERSISTENTLY full
    # or sending is PERSISTENTLY blocked beyond the threshold. This is the implementation of
    # "slow client's _ws_active returns to baseline within the heartbeat timeout after disconnect"
    # (ISSUE #73 acceptance #3): can't rely on TCP to discover half-open connections (may take
    # minutes), must judge death actively.
    watchdog = asyncio.create_task(_ws_watchdog(ws, outbound))

    # The register action is protected by try/finally: any exit path between successful
    # registration and unregistration must run the finally's unregister — otherwise the hub keeps
    # enqueuing to a closed connection (bounded queue won't leak memory, but client_count stays
    # falsely high).
    try:
        await ws_hub.register(outbound)
        # Wait for client disconnect (the watcher returns on receive of disconnect).
        # A hub disconnect (Redis down) no longer affects a single connection: _consume_loop has
        # its own backoff-reconnect, and the connection just temporarily stops receiving
        # incremental frames — more stable than the old "Redis hiccup → all connections exit and
        # reconnect".
        await disconnect
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        # [This whole block's style was forced out by a bug; don't change it to "the convenient
        # look"]
        #
        # This function may enter finally due to TASK CANCELLATION (uvicorn shutdown, TestClient
        # context exit both do). When a task is already in cancelled state, ANY unprotected await
        # in finally may immediately raise CancelledError and interrupt the entire finally block
        # — minimal repro:
        #     try: await sleep(999)
        #     finally: await x(); n -= 1      # x() raises → n -= 1 never runs
        # If written naively at the very end (this file's first version did), once
        # `outbound.aclose()` raises, `_ws_active -= 1` is skipped, and the count only ever grows
        # — exactly the "false data" this ticket fixes.
        #
        # So the rule is: wrap each cleanup action in its own try, put the count balancing first
        # and make it independent of any cleanup success. Reclaiming the count is a hard promise
        # (ISSUE #73 acceptance #3) and must not have a chance to be skipped.

        # ① Balance the count first. No await here, so it CANNOT be interrupted by cancellation.
        #    Must pair with the +1 above: missing it would make the count only grow on abnormal
        #    client disconnect (browser crash / network switch), and the panel would say
        #    "47 connections" while only 3 big screens are actually open — false data is worse
        #    than no data.
        _ws_active -= 1
        log.info("ws disconnected", extra={"ws_active": _ws_active})

        # ② Record this connection's backpressure facts into metrics (sync, no await, also
        #    uninterruptible).
        metrics.telemetry_frames_dropped.inc(outbound.telemetry_dropped)
        metrics.alerts_deferred.inc(outbound.alerts_deferred)
        if outbound.telemetry_dropped or outbound.alerts_deferred:
            log.warning(
                "ws backpressure on close: telemetry_dropped=%d alerts_deferred=%d",
                outbound.telemetry_dropped,
                outbound.alerts_deferred,
            )

        # ③ Unregister from the hub (ASAP: starting next tick, no more enqueue to this
        #    connection). Each await is in its own try; any failure doesn't affect other
        #    cleanups or the ① count.
        try:
            await ws_hub.unregister(outbound)
        except BaseException:  # noqa: BLE001 —— includes CancelledError, see header comment
            log.debug("ws hub unregister failed during teardown", exc_info=True)

        # ④ Stop tasks, stop the send coroutine. Order: stop the send-coroutine-related tasks
        #    first, then close outbound — each await in its own try; any failure doesn't affect
        #    other cleanups or the ① count.
        watchdog.cancel()
        disconnect.cancel()
        for task in (watchdog, disconnect):
            try:
                await asyncio.gather(task, return_exceptions=True)
            except BaseException:  # noqa: BLE001 —— includes CancelledError, see header comment
                pass
        try:
            await outbound.aclose()
        except BaseException:  # noqa: BLE001
            log.debug("ws outbound close failed during teardown", exc_info=True)
