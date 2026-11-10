"""Prometheus metric exposure (backend ingest layer).

Why this file is needed (previously a classic "cloned but not wired up" case):
  `prometheus/client_python` was cloned into `external/` in the previous round,
  but **nothing in `backend_ingestion/` imported it** — without metric export,
  no matter how many Prometheus scrape targets you configure you only get `up=0`.

  This module exports the runtime counters already exposed in `/healthz` as
  Prometheus metrics as well, so `up{job="robot_backend"}` and
  `robot_backend_queue_depth` etc., declared in
  `infra/observability/prometheus.yml`, **actually exist**.

Metric names align with the PromQL in `infra/observability/rules/robot-alerts.yml`:
  robot_backend_queue_depth          (Gauge, rule uses `> 5000`)
  robot_backend_mqtt_dropped_total   (Counter, rule uses increase(...[1m]))

This module only defines metrics; it does not collect. WS-related metrics have
two sources — **do not confuse them**:
  - `robot_backend_ws_connections` —— current server-side connection count
    (order 16 T1b), incremented/decremented by `main.py`'s `ws_robots` on
    connect/disconnect;
  - `robot_backend_ws_probe_alive` / `robot_backend_ws_probe_failures_total`
    —— WS **synthetic probe** (client perspective), updated by the loop in
    `app/ws_probe.py`.
They are complementary, not duplicate: a connection count of 1 only proves "a
client is hanging on"; a probe of 1 proves "this connection really still receives
the first snapshot frame".

Single source of naming (order 32 / ISSUE #100, folding in ISSUE #72's
convergence action):
  Historically the order-16 implementation branch stood up a separate
  `robot_monitor_*` prefix (6 metrics) that **coexisted** with the already-shipped
  `robot_backend_*` from order 19, of which 3 were duplicate counters of the same
  source. This module converges to `robot_backend_*` as the **sole naming**. The 12
  old names below (6 native + 6 second-wave) are retired; this ticket (order 32),
  on top of `main@8cbe4f7`, adds the **runtime hard gate**: previously "old names
  are gone" was only backed by comments and a guard unit test, and that test used
  render() on a self-contained registry — not the same bytes as the real
  `/metrics`.
    - `robot_monitor_batch_queue_size`         -> deleted (same source: `robot_backend_queue_depth`)
    - `robot_monitor_batch_rows_total`         -> deleted (same source: `robot_backend_telemetry_written_total`)
    - `robot_monitor_mqtt_messages_total`      -> deleted (same source: `robot_backend_mqtt_received_total`)
    - `robot_monitor_batch_flush_total{reason}`-> renamed into `robot_backend_batch_flushes_total{reason}`
    - `robot_monitor_ws_connections`           -> renamed to `robot_backend_ws_connections`
    - `robot_monitor_history_latency_seconds`  -> renamed to `robot_backend_history_latency_seconds`
  The 3 deleted ones could be deleted because prometheus_client's Counter/Gauge
  have **idempotent accumulate/overwrite** semantics: after removing the duplicate
  definition, the same event is expressed by only one counter — values are neither
  lost nor doubled.

Second-wave convergence (2026-10-11, CI record): the #73/#76/#77 reclamation
branches were built on an old main, unaware of each other's naming constraints;
after merge, 6 old-prefix metrics re-entered, and #72's guard test
(FORBIDDEN_MONITOR_PREFIX, a line-start prefix assertion) caught them on the
spot — exactly what the guard exists for.
    - `robot_monitor_robots_online`                     -> `robot_backend_robots_online`
    - `robot_monitor_history_source_total`              -> `robot_backend_history_source_total`
    - `robot_monitor_ws_outbound_queue_depth`           -> `robot_backend_ws_outbound_queue_depth`
    - `robot_monitor_ws_telemetry_frames_dropped_total` -> `robot_backend_ws_telemetry_frames_dropped_total`
    - `robot_monitor_ws_alerts_deferred_total`          -> `robot_backend_ws_alerts_deferred_total`
    - `robot_monitor_ws_slow_client_evicted_total`      -> `robot_backend_ws_slow_client_evicted_total`
  None of these metrics are yet referenced by any dashboard/alert rule, so a pure
  rename carries no downstream compatibility burden.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

# Separate registry: avoid polluting the process's other libraries' default
# registries, and let tests grab a clean instance (the default REGISTRY is a
# global singleton; repeated imports raise duplicate-registration errors).
registry = CollectorRegistry()

telemetry_written = Counter(
    "robot_backend_telemetry_written_total",
    "Telemetry rows successfully written to TimescaleDB (accumulated per batch, unit: rows)",
    registry=registry,
)
# Labeled by trigger reason — the only evidence that the "dual trigger (500 rows /
# 1s)" is working as designed.
#   count   : triggered by accumulating batch_trigger_count (500) rows -> high inflow rate, queue never drained
#   timeout : triggered by exceeding batch_trigger_seconds (1.0s) -> low inflow rate, flush via fallback
# Under a steady load test count should dominate; if timeout dominates, 1000 msg/s
# was not reached.
# The label values are only these two **constant enums** (plus 'unknown' after
# dirty-value normalization) — no high-cardinality risk (contrast: never label by
# robot_id).
#
# ISSUE #72: this metric absorbed the old `robot_monitor_batch_flush_total` — the
# latter had a reason label, the former did not; two time series for "one flush
# event". After merging we keep the label capability (no information loss) and only
# one name (PromQL/dashboards/alerts no longer maintain two sets). The metric name
# reuses the already-live `robot_backend_batch_flushes_total`, so the PromQL-side
# `sum(rate(...))` form needs no change.
batch_flushes = Counter(
    "robot_backend_batch_flushes_total",
    "BatchWriter flush count, split by trigger reason (count=full 500 rows / timeout=full 1s)",
    labelnames=("reason",),
    registry=registry,
)
queue_depth = Gauge(
    "robot_backend_queue_depth",
    "Current backlog of the asyncio.Queue (steadily rising = flush cannot keep up with 1000 msg/s)",
    registry=registry,
)
mqtt_received = Counter(
    "robot_backend_mqtt_received_total",
    "Telemetry messages received over MQTT",
    registry=registry,
)
mqtt_dropped = Counter(
    "robot_backend_mqtt_dropped_total",
    "Messages dropped due to MQTT validation failure (>0 means the payload contract is broken)",
    registry=registry,
)

# ---- WS synthetic probe (gap G-3 "dashboard's fake green") ----
# Note: these two are **probe-perspective** metrics, semantically different from
# `robot_backend_ws_connections` (server-side connection count, order 16 T1b);
# do not overwrite or reuse one name for both.
ws_probe_alive = Gauge(
    "robot_backend_ws_probe_alive",
    "Latest WS synthetic-probe result (1=handshake + received snapshot first frame / 0=failure)",
    registry=registry,
)
ws_probe_failures = Counter(
    "robot_backend_ws_probe_failures_total",
    "Cumulative WS synthetic-probe failures (>0 means the client-perspective realtime stream dropped)",
    registry=registry,
)

# ---- order 16 T1b: guardrail visualization panel (ISSUE #72 folded into the single robot_backend_* naming) ----
# The two hard constraints in AGENTS.md §3 (no row-by-row INSERT on the backend /
# no row-by-row re-render on the frontend) were previously only in docs; each
# metric below maps to one "by what evidence" question an enterprise acceptance
# review could ask.
# No duplication with the metrics above: ws_connections / history_latency_seconds
# are genuinely new capabilities in this ticket.

# Current WS connection count. Gauge not Counter: a big screen disconnects and
# reconnects, so the count goes up and down.
# Use: verify that "100 robots" are really being watched by N big screens (count
# should be number of screens x 1 per screen), and hunt connection leaks (only
# increasing = a disconnected WS never decremented in its finally block).
# Refresh action is in main.py's /metrics (under the pull model nobody sets it for us periodically).
ws_connections = Gauge(
    "robot_backend_ws_connections",
    "Current active /ws/robots connection count (including in-handshake connections not yet authenticated)",
    registry=registry,
)

# Currently online count (ISSUE #77). The semantics is "time since last telemetry
# <= robot_offline_threshold_s", NOT "status_code != 0" — a robot that stopped
# reporting freezes its status_code at the last frame, so counting it would
# steadily over-report. Refresh point: `/healthz` (the orchestrator hits it at
# second-level frequency and maintains it as a side effect, no separate timer).
robots_online = Gauge(
    "robot_backend_robots_online",
    "Online robot count (now - last_seen <= threshold, ISSUE #77; stopped reporters excluded)",
    registry=registry,
)

# /history latency distribution. Histogram not Gauge: we want p50/p95/p99, not an
# instantaneous value.
# Bucket boundaries: opening a Drawer is **user interaction**, 1s is the perceived
# threshold; below 10ms is the local-cache-hit magnitude.
# If the upper bound at 2.5s still piles up, the query is dragging down interaction
# and needs to go through the continuous-aggregate table rather than raw time_bucket.
history_latency_seconds = Histogram(
    "robot_backend_history_latency_seconds",
    "/history endpoint processing time (incl. DB query, the data source for Drawer line chart + heatmap)",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=registry,
)

# /history data-source counter (ISSUE #76).
#
# Why it must exist: after switching to the continuous aggregate, p95 drops, but
# "faster" alone cannot self-prove — empty results, cache hits, even a wrongly
# written query would shorten the time. Splitting the source into cagg/raw two
# series means p95 dropping AND `source="cagg"` share rising must both hold to be
# truly effective; conversely if `source="raw"` dominates long-term, the aggregate
# is not keeping up (refresh strategy or scheduling problem) — exactly the kind of
# regression this metric was built to catch, rather than by human eyes.
# Only 2 label values, bounded cardinality (contrast mqtt_received deliberately
# not carrying robot_id, same reason).
history_source = Counter(
    "robot_backend_history_source_total",
    "/history actual data source hit: cagg=continuous aggregate robot_history_10s / raw=raw table fallback",
    ("source",),
    registry=registry,
)


def render() -> tuple[bytes, str]:
    """Return (body, content_type) for direct use by FastAPI's /metrics endpoint."""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def observe_flush(reason: str, rows: int) -> None:
    """Record one flush: batch count (with reason) + rows.

    `reason` only accepts 'count' / 'timeout' (decided by the caller, see
    batch_writer._flush). Unknown values are not silently swallowed — they become
    'unknown' so they surface on the dashboard; otherwise a typo (e.g. 'Count')
    would drop a panel block with nobody noticing.
    """
    if reason not in ("count", "timeout"):
        reason = "unknown"
    batch_flushes.labels(reason=reason).inc()
    telemetry_written.inc(rows)


def refresh_gauges(queue_size: int, ws_conns: int) -> None:
    """Refresh Gauge instantaneous values before exposing /metrics.

    Why this must be explicit: Prometheus is a **pull** model — the scraper only
    reads values at scrape time, it does not set them for us periodically. If a
    Gauge is not refreshed it stays frozen at the last set() moment (appearing as a
    "constant 0", exactly the phenomenon order-16 acceptance wants excluded).
    Counter / Histogram have no such problem (accumulate semantics, the call site
    is at the event).
    """
    queue_depth.set(queue_size)
    ws_connections.set(ws_conns)


# ---------------------------------------------------------------------------
# ISSUE #73: WS outbound backpressure + heartbeat
#
# Why a separate group instead of reusing robot_monitor_ws_connections: the
# connection count answers "how many big screens are hanging on", **not** "are
# they keeping up". A slow client (mobile network / frozen tab) still increments
# the count by 1 yet piles 1000 msg/s of outbound frames in its own queue. The
# three metrics below are the observability surface for "who is dragging":
#   - outbound_queue_depth  Gauge   per-connection aggregated outbound backlog (persistent >0 = someone can't keep up)
#   - telemetry_frames_dropped_total  Counter  telemetry frames **dropped** (the cost of bounded memory)
#   - alerts_deferred_total           Counter  alerts deferred because the queue was full (>0 = pathological,
#                                           0 is evidence the "alerts must not be lost" constraint holds)
# Deliberately no connection label: connections are short-lived objects; labeling
# by connection would grow Prometheus series without bound (every reconnect is a
# new series). Drill down via logs when needed.
# ---------------------------------------------------------------------------

# Outbound backlog (one WS connection per machine; here we take the **sum across
# all connections**).
outbound_queue_depth = Gauge(
    "robot_backend_ws_outbound_queue_depth",
    "Combined outbound backlog frame count across all /ws/robots connections (persistent >0 means a slow client)",
    registry=registry,
)

# Telemetry frames dropped. **Telemetry may be dropped, alerts may not** (ISSUE #73
# forbidden item #3): this counter rising is in-design backpressure behavior, not a
# fault; it rising while alerts_deferred_total stays 0 precisely proves graded
# handling works as intended.
telemetry_frames_dropped = Counter(
    "robot_backend_ws_telemetry_frames_dropped_total",
    "Telemetry frames dropped due to a full outbound queue (drop oldest, the backpressure cost of a slow client)",
    registry=registry,
)

# Deferred alerts. Design target is always 0: on overflow the queue is allowed to
# grow (see ws_backpressure); only a pathological pile-up of thousands of queued
# alerts would push this above 0.
alerts_deferred = Counter(
    "robot_backend_ws_alerts_deferred_total",
    "Alert frames deferred because the outbound queue was full (should be 0; >0 means alert backlog, scale up or drop a connection)",
    registry=registry,
)

# Slow/half-open connections evicted. Complements the connection-count Gauge: the
# count dropping is the **result**, this counter is the **cause** — ops use it to
# tell "big screen closed normally" from "kicked by the server".
ws_slow_client_evicted = Counter(
    "robot_backend_ws_slow_client_evicted_total",
    "Slow client connections actively closed due to a persistently full outbound queue or heartbeat timeout",
    registry=registry,
)

# ---------------------------------------------------------------------------
# ISSUE #71: WS outbound fan-out hub (server-side broadcast coalescing)
#
# Why a separate group: #73's metrics answer "is a single connection keeping up",
# these two answer "**is the server** still scaling linearly with client count":
#   - frames_total{kind}     actual WS frames **sent** (incl. one copy per connection).
#     before the change ~= N x 1000/s (each connection subscribes independently + sends individually);
#     after ~= N x (tick x robot count), serialization done once per process.
#   - coalesced_total        telemetry frames **merged away** within a tick (intermediate frames overwritten).
#     only > 0 proves "keep only the latest frame per robot" really works;
#     staying 0 means the tick did nothing (every received frame was sent individually).
# No ws_hub_clients: connection registration maps 1:1 with ws_robots' _ws_active;
# emitting another same-source Gauge would resurrect the dual-track naming #72 just cleared.
# ---------------------------------------------------------------------------
ws_hub_frames_total = Counter(
    "robot_backend_ws_hub_frames_total",
    "WS hub frames actually sent, split by kind (telemetry=one merged frame per robot / alert=sent individually)",
    labelnames=("kind",),
    registry=registry,
)
ws_hub_coalesced_total = Counter(
    "robot_backend_ws_hub_coalesced_total",
    "Telemetry frames merged away by the tick (received this tick - actually fanned out; >0 = keep-latest-per-robot in effect)",
    registry=registry,
)

# ---------------------------------------------------------------------------
# Benchmark C: offline buffer (store-and-forward)
#
# Rows buffered when TimescaleDB is down, replayed after DB recovery.
#   - telemetry_buffered_total          : rows stored in the buffer (cumulative)
#   - telemetry_buffered_replayed_total : rows actually replayed and re-enqueued (cumulative)
# If their difference stays persistently > 0, it means "buffer keeps growing but
# replay never runs" = Redis<->DB inconsistency, or the replay loop died. Without
# this metric you cannot prevent the "buffer piles up but the dashboard still shows
# green" false-green (same context as G-4).
# ---------------------------------------------------------------------------
telemetry_buffered = Counter(
    "robot_backend_telemetry_buffered_total",
    "Telemetry rows stored in the offline buffer (Redis list) after a DB write failure (awaiting replay)",
    registry=registry,
)
telemetry_buffered_replayed = Counter(
    "robot_backend_telemetry_buffered_replayed_total",
    "Telemetry rows replayed from the offline buffer and re-enqueued into the writer",
    registry=registry,
)
