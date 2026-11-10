"""Environment-variable configuration (pydantic-settings).

Maps to spec/00 SS6 and .env.example. All thresholds/windows live here; never
hard-code them in business code.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- MQTT ----
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379"

    # ---- TimescaleDB ----
    database_url: str = "postgresql://postgres:postgres@localhost:5432/robot_monitor"

    # ---- Batch flush dual trigger (spec/01 SS4.2) ----
    batch_trigger_count: int = 500
    batch_trigger_seconds: float = 1.0
    batch_queue_maxsize: int = 20000
    # Per-batch failure retries and backoff (see the degradation chain in
    # services/batch_writer.py).
    batch_flush_max_retries: int = 3
    batch_flush_retry_backoff_s: float = 0.5
    # Binary-search isolation depth cap for a sick batch: each level halves the
    # batch, segment size = batch/2^depth. The cap bounds the worst-case number
    # of round-trips (avoids O(n) queries).
    # Depth 8 + batch 500 -> minimum segment 1 row, enough to localize a single
    # bad row.
    batch_isolation_max_depth: int = 8

    # ---- asyncpg connection pool ----
    # Idle floor (asyncpg 0.32 semantics, see app/db/pool.py). The write path is
    # a single connection flushing sequentially, so 2 is enough; `db_pool_max_size`
    # leaves headroom for concurrency (/healthz probe + read path arrive together).
    # Note: do NOT use the order-08 branch's "DB_POOL_SIZE doubles as
    # init_size/min_size/max_size" approach -- equal three values degrade the pool
    # to a semaphore (see the MAX_POOL_SIZE comment in app/db/pool.py for local
    # measurements).
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    db_command_timeout_s: float = 30.0
    db_max_idle_s: float = 300.0
    # Pool-build timeout (seconds). **Must be bounded**, reason in app/db/session.py's
    # module header comment: asyncpg 0.32's init_size means "connect at pool build",
    # at which point `connection.connect()`'s default timeout=60 takes effect -- if
    # the DB is not ready when building the pool, every call blocks the full 60s.
    # And this is the **probe path** (/healthz ping()), so blocking 60s means the
    # probe hangs forever and db=up never arrives. Give a bound far shorter than any
    # probe cycle so failures surface fast and retry fast.
    # Measured 0.32.0: init_size=2 all-unreachable -> without this param, 60.02s
    # TimeoutError; with timeout=8 -> 8.01s raised (see tests/test_db_pool_timeout.py).
    db_connect_timeout_s: float = 8.0
    # /healthz DB probe (a ready probe should not hit the DB every time; cache result)
    db_health_cache_s: float = 5.0

    # ---- Redis latest-state cache (spec/01 SS4.3) ----
    state_ttl_seconds: int = 10

    # ---- robot_metadata heartbeat (G-6 / ISSUE #96) ----
    # Master switch. When off, metadata_writer's run() exits immediately and
    # flush() returns immediately: it neither rewrites robot_metadata nor does the
    # offline scan; the telemetry write path (batch_writer) semantics and
    # throughput are completely unaffected -- this is the literal "must not change
    # the existing telemetry write path".
    metadata_heartbeat_enabled: bool = True
    # metadata heartbeat flush period. Must be <= state_ttl_seconds semantically:
    # `robot:state:{id}` lives 10s in Redis, so if the heartbeat is slower, there
    # is a window where "Redis already expired but DB is_online is still true".
    # 1s also caps the write amplification at <=100 rows/second.
    metadata_flush_seconds: float = 1.0

    # ---- Online-status threshold (ISSUE #77) ----
    # "Alive right now" is decided only by the backend: the frontend used to guess
    # online from `status_code === 0`, so a robot that stopped reporting froze its
    # last status_code and showed online forever.
    #
    # The 30s basis (not a guess): normal reporting is 10Hz (one every 100ms),
    # 30s = 300 consecutive lost frames, far larger than the scale of network
    # jitter / MQTT reconnect / JS event-loop blocking; and AGENTS.md acceptance
    # requires "avoid misjudging 10Hz normal jitter". **Deliberately different**
    # from alert_engine's offline_after_s (10s): that is an **alert threshold**
    # (earlier report = earlier response), this is a **display threshold** (later
    # flip = no flicker). The two have different semantics; do not collapse them
    # into one value just to "look consistent".
    robot_offline_threshold_s: float = 30.0

    # ---- CORS (R-17) ----
    # Allowed origins, comma-separated. **Default empty = CORS middleware off**
    # (same-origin deployment suffices); never default to `*`: `*` combined with
    # allow_credentials=True lets any site hit the backend with the user's
    # credentials (CSRF / info-leak surface), explicitly forbidden in R-17 SS6.
    # Only an **explicit** `*` opens all origins (a separate code branch + warning
    # log).
    # Example: CORS_ALLOW_ORIGINS=http://localhost:5173,https://board.example.com
    cors_allow_origins: str = ""

    # ---- Rate limiting (R-17, optional, default off) ----
    # Master switch. Default false = no rate-limiting logic at all; existing
    # e2e/probe/monitoring request cadence is byte-for-byte unchanged
    # (R-17 SS6 hard gate: rate limiting must not break e2e-30k-landing's real green).
    rate_limit_enabled: bool = False
    # Per-client-IP request cap per minute (fixed window). Only applies when
    # rate_limit_enabled=true. /healthz, /metrics, /ws/robots and /internal/* are
    # always exempt (see security_middleware).
    rate_limit_per_minute: int = 120

    # ---- HTTP ----
    ws_host: str = "0.0.0.0"
    ws_port: int = 8000

    # ---- WS synthetic probe (gap G-3 "dashboard's fake green") ----
    # The probe is an **observer**: disabling it, or it crashing, does not affect
    # telemetry writing or WS broadcast. On by default because "without it there is
    # no client perspective"; an enterprise intranet only needs loopback, no
    # external dependency.
    ws_probe_enabled: bool = True
    # Probe interval. The lower bound of 30s is clamped by `ws_probe.py:MIN_INTERVAL_S`
    # -- configuring smaller here won't go faster (the probe itself is a WS client
    # and would contend with 1000 msg/s on loopback).
    ws_probe_interval_s: float = 60.0
    # Probe target path. Made configurable so a future "multiple WS" needs no code
    # change.
    ws_probe_path: str = "/ws/robots"
    # Startup grace: uvicorn's lifespan startup creates the probe task, but the
    # port is not yet accepting, so an immediate probe is doomed. Give a window to
    # avoid an opening false-failure.
    ws_probe_startup_grace_s: float = 5.0

    # ---- WS outbound backpressure + heartbeat (ISSUE #73) ----
    # One **bounded** outbound queue per connection: the relay loop only does
    # put_nowait (never awaits); slow clients no longer stall the Redis relay loop,
    # and worst-case memory = queue length x frame size (a constant).
    # Basis: 100 units x 10Hz = 1000 msg/s; if you await per-frame into a slow
    # client, one stuck browser tab can drag the whole process's memory unbounded
    # (see app/ws_backpressure.py header comment).
    #
    # Value 500: about 0.5s of telemetry (1000 msg/s) -- enough to absorb mobile
    # network jitter, but not so large that "dropping the oldest frame" is delayed
    # until user-visible. Configuring larger returns to unbounded.
    ws_outbound_queue_size: int = 500
    # Heartbeat sentinel cadence. The sender drops one each loop iteration; it gets
    # carried through the real backpressure/disconnect judgment by `await send_json`
    # -- so "it got enqueued" is itself a liveness check.
    # A browser-side half-open connection (cable unplugged but TCP not down) shows
    # up as send blocking, and that connection is then reaped by
    # `ws_outbound_close_timeout_s`.
    ws_heartbeat_interval_s: float = 15.0
    # Max tolerance for a single connection with a persistently-full queue /
    # persistently-blocked send, after which it is actively closed. The cap exists
    # so "a disconnected slow client's counter returns within the heartbeat
    # timeout" becomes a verifiable promise, not "wait for TCP to notice itself"
    # (which can take minutes). Default 60s ~ 3 heartbeat cycles.
    ws_outbound_close_timeout_s: float = 60.0
    # When the outbound queue is full, how many extra slots may expand for the
    # **undroppable alerts** (see ws_backpressure).
    ws_alert_overflow_slack: int = 64

    # ---- WS broadcast coalescing beat (ISSUE #71) ----
    # Server-side outbound hub fan-out period. 100ms = 10Hz, on the same beat as
    # upstream telemetry rate (100 units x 10Hz) and the frontend 100ms throttle
    # (spec/03 SS6): each beat fans out only the **latest frame** per robot;
    # intermediate frames are naturally coalesced in the "overwrite" (counter
    # ws_hub_coalesced_total is the evidence).
    # Larger -> cheaper server, but duller dashboard updates; smaller -> more
    # real-time, but serialization/send counts climb back.
    ws_broadcast_interval_ms: float = 100.0

    # ---- Auth / RBAC (order 15) ----
    # JWT signing secret. **Must** be injected via .env, no default -- an empty
    # default equals shipping a secret anyone can sign with, so anyone with the
    # source can forge an admin token (AGENTS.md SS5).
    # When unset, AppSettings construction raises; better to fail to start than to
    # silently degrade into "anyone can enter".
    jwt_secret: str
    # Token lifetime: 8 hours = one shift. The big screen is a 7x24 public
    # terminal; after expiry the frontend 401 interceptor clears the token and
    # returns to login, for the on-duty person to log in again.
    jwt_lifetime_seconds: int = 8 * 3600

    # ---- OpenTelemetry (order 16 T3) ----
    # Empty = **off** (default). A factory intranet often has no OTLP collector,
    # so empty is no-op and never fails to start from being unconfigured (see the
    # layered degradation in app/telemetry_tracing.py).
    # Filling `http://collector:4318` enables it; this module completes the
    # /v1/traces path.
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "robot_backend"

    # ---- Redis channels (spec/00 SS5.2/SS5.4) ----
    chan_stream_all: str = "robot:stream:all"
    chan_stream_one: str = "robot:stream:{robot_id}"
    chan_alerts: str = "robot:alerts"

    # ---- Offline buffer (competitive benchmark C: store-and-forward, cf. Netdata/ORO) ----
    # When TimescaleDB fails, batch_writer used to drop rows (rejected); now they
    # are serialized into a Redis backup queue and replayed once the DB recovers.
    # This closes the centralized-deployment blind spot that network isolation
    # would otherwise create.
    # On by default. If Redis is unavailable, safe fallback: the caller counts
    # them as the original rejected.
    offline_buffer_enabled: bool = True
    offline_buffer_key: str = "robot:buffer:telemetry"
    # Capacity cap: 1000 msg/s x 200s worth. Beyond that the oldest items are
    # trimmed (prevents memory runaway).
    offline_buffer_maxlen: int = 200_000
    # Replay loop period (seconds). If the buffer is empty it does not even ping
    # (zero overhead).
    offline_buffer_replay_interval_s: float = 5.0

    @property
    def robot_ids(self) -> list[str]:
        """100 robot IDs (consistent with the pre-seeded robot_metadata in init.sql)."""
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
