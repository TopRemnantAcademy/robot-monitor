"""Alert engine configuration (all thresholds and windows centralized here, spec/02 §3)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env", env_file_encoding="utf-8", extra="ignore"
    )

    redis_url: str = "redis://localhost:6379"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/robot_monitor"

    # ---- DB connection pool (order 17 found: db.py has been reading these two keys, but config didn't define them) ----
    # `app/db.py:get_pool()` references `settings.db_pool_size` / `settings.db_command_timeout`,
    # but this file previously had **no** such fields -- so any path reaching `get_pool()` (alert
    # persistence `store.save`, metadata updates, this order's escalation scan) would throw
    # `AttributeError: 'Settings' object has no attribute 'db_pool_size'`.
    # This is a config item missed when order 08 introduced `app/pool.py`; this order adds it to get
    # escalation running.
    # Named to align with `backend_ingestion/app/config.py` (db_pool_min_size, etc.),
    # but pool.py's parameter name is `pool_size`, hence `db_pool_size` here.
    db_pool_size: int = 2          # pre-built count = idle floor (see app/pool.py:MAX_POOL_SIZE)
    db_command_timeout: float = 30.0
    # Pool-build timeout (seconds); semantics and default match backend_ingestion/app/config.py:db_connect_timeout_s.
    # If not set, init_size's "pre-connect on pool creation" brings in connection.connect()'s default 60s,
    # hanging the startup/liveness path when the DB is not ready (see app/pool.py:create_pool's connect_timeout comment).
    db_connect_timeout_s: float = 8.0

    # ---- Redis channels (consistent with backend convention) ----
    chan_stream_all: str = "robot:stream:all"
    chan_alerts: str = "robot:alerts"

    # ---- motor_temp (spec/02: >75°C for >=3 samples, severity 3, dedup 60s) ----
    motor_temp_c: float = 75.0
    motor_temp_min_samples: int = 3
    motor_temp_dedup_s: int = 60

    # ---- battery_low (<15%, severity 2, dedup 120s) ----
    battery_low_pct: float = 15.0
    battery_low_dedup_s: int = 120

    # ---- offline (no telemetry >10s, severity 3, dedup 30s) ----
    offline_after_s: float = 10.0
    offline_dedup_s: int = 30
    # Criterion source switch (ISSUE #96): when True, offline judgment prefers `robot_metadata.is_online`
    # (batch-maintained by the backend metadata_writer heartbeat), falling back to the `last_seen`
    # time delta only when that value is unavailable. False always uses the time delta -- the switch is
    # kept for "one-line rollback", not because the two semantics are equivalent (they are not; rolling
    # back reproduces the old behavior).
    offline_use_is_online: bool = True

    # ---- joint_stuck (angle variance ~0 within 5s but torque over threshold, severity 2, dedup 60s) ----
    joint_stuck_window_s: float = 5.0
    joint_stuck_variance: float = 1e-6
    joint_stuck_torque: float = 8.0
    joint_stuck_dedup_s: int = 60

    # ---- inhibition (offline, compared with Alertmanager's inhibit_rules) ----
    # When a robot is judged offline (severity 3), inhibit its other alerts on the same robot_id:
    # once the machine is offline, "low battery" / "joint stuck" have no remediation value -- the root
    # cause IS the offline state.
    # The switch is kept for easy rollback and A/B comparison; turning it off restores pre-inhibition behavior.
    inhibit_offline: bool = True

    # ---- inhibition (severity layer, order 30) ----
    # "A higher-severity alert inhibits lower-severity alerts on the same robot": when a sev3 active alert
    # exists on the same machine, its sev2/sev1 alerts are neither persisted nor published (the root cause
    # is upstream; derivative symptoms have no remediation value).
    # Decoupled from inhibit_offline: either layer's A/B can be rolled back independently, convenient for
    # locating which layer blocked an alert.
    # Threshold see app/inhibition.py:MIN_SOURCE_SEVERITY (currently 3).
    inhibit_severity: bool = True

    # ---- escalation (aligned with Alertmanager's repeat_interval) ----
    # severity=3 and past this many seconds without ack -> set escalated=TRUE and re-send once.
    # 300s (5 minutes) comes from work order T2; it is an empirical scale of "the on-duty operator should
    # have responded" -- shorter would nag too hard (a single on-site inspection can exceed 1 minute),
    # longer would lose the point of escalation.
    escalate_after_s: int = 300
    # Escalation scan interval. 30s is a compromise of "timeliness vs scan cost":
    #   - under the 300s threshold, a 30s jitter delays escalation by at most 30s, no material effect on nagging;
    #   - the predicate is covered by the partial index idx_alert_escalation (init.sql:206),
    #     each scan touches a narrow set, so scanning every 30s is negligible overhead.
    escalate_scan_interval_s: float = 30.0
    # Max re-send escalations per alert. Prevents "nobody acts -> forever re-send every 5 minutes" from
    # becoming a long-term noise source (Alertmanager also needs repeat_interval rather than infinite resend).
    escalate_max_repeat: int = 3

    # ---- judgment loop ----
    tick_interval_s: float = 1.0        # offline ticker interval
    window_maxlen: int = 100            # sliding-window capacity per robot (10Hz -> ~10s)

    # ---- anomaly (statistical anomaly detection, benchmarked against Netdata edge ML / ORO incident remediation) ----
    # Existing rules are static thresholds (>75°C, <15%, variance~0). This rule computes a rolling z-score
    # over the window, capturing "mutations relative to the robot's own baseline": battery plunge, joint
    # temperature step, torque spike, etc. -- the "slow drift + mutation" anomalies that static thresholds
    # miss. z=4 ~= 4 sigma, extremely low false-positive rate.
    anomaly_zscore: float = 4.0         # trigger threshold (|z| >= this value)
    anomaly_min_std: float = 0.5        # minimum std-dev gate: fluctuation below this is treated as normal noise, not reported
    anomaly_min_samples: int = 20       # window sample floor (<20 not evaluated, ~2s)
    anomaly_dedup_s: int = 120         # dedup window per robot per metric (seconds)

    # ---- alert notification (I-14) ----
    # Factory-side robot webhook: if empty, the whole notification chain is silently skipped (off by
    # default, to avoid misconfiguration spamming).
    # The URL contains a token, **injected only from an env var, never written into the repo**.
    alert_webhook_url: str = ""
    # dingtalk | wecom | generic (generic JSON, handed to the factory's own forwarder for processing)
    alert_webhook_kind: str = "generic"
    # Push threshold: default 3 -- only push critical, preventing "low-battery storm" spam
    alert_webhook_min_severity: int = 3

    # ---- Prometheus ----
    # The alert engine is not a FastAPI app and has no HTTP route to mount /metrics on,
    # so app/metrics.py:start() starts an http.server on a separate port (order 13 wiring).
    # Separated from the backend ingestion service port to avoid scrape-target conflicts.
    metrics_port: int = 8001

    @property
    def robot_ids(self) -> list[str]:
        return [f"humanoid-{i:03d}" for i in range(1, 101)]


settings = Settings()
