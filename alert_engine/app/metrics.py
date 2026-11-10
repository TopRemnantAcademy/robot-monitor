"""Prometheus metrics exposure for the alert engine.

Why the alert engine needs a **separate** metrics server:
  `alert_engine/app/main.py` is a pure asyncio process (`asyncio.run(amain())`),
  and it runs **no HTTP framework** -- no FastAPI, no uvicorn.
  So we cannot mount a `/metrics` route on FastAPI like the backend does;
  we must start a tiny HTTP server to emit `prometheus_client`'s output,
  otherwise the `robot_alert_engine` job in `prometheus.yml` is forever `up=0`
  -> the `AlertEngineDown` rule **permanently false-alarms**.

Implementation uses the standard-library `http.server` on a separate thread:
  - No new dependencies like aiohttp/uvicorn (keep the alert engine's dependency surface minimal)
  - Does not contend with the asyncio event loop (`ThreadingHTTPServer` single worker, read-only registry)
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from prometheus_client.exposition import CONTENT_TYPE_LATEST

log = logging.getLogger("metrics")

registry = CollectorRegistry()

alerts_emitted = Counter(
    "robot_alert_emitted_total",
    "Number of alerts successfully emitted (persisted + published)",
    ["rule_id"],
    registry=registry,
)
alerts_suppressed = Counter(
    "robot_alert_suppressed_total",
    "Number of alerts suppressed by inhibition (order 10 offline layer + order 30 severity layer)",
    ["rule_id", "reason"],
    registry=registry,
)
alerts_silenced = Counter(
    "robot_alert_silenced_total",
    "Number of alerts silenced (within TTL) and therefore not persisted/published (order 17)",
    ["rule_id"],
    registry=registry,
)
alerts_escalated = Counter(
    "robot_alert_escalated_total",
    "Number of alerts re-sent by escalation (severity=3 unacked past timeout, order 17)",
    ["rule_id"],
    registry=registry,
)
# Online robot count (order 32 / ISSUE #100 rename): originally `robot_alert_robots_online`.
#
# Why rename: the `_gauge` suffix is **type self-evidence**, not decoration. The same-name problem
# really happened -- the root cause of ISSUE #72 was "the same online count" having one name in each of
# two processes (backend `robot_backend_robots_online`, alert engine `robot_alert_robots_online`),
# so panels/alerts had to maintain two sets of PromQL, and **nobody could tell from the name which is a
# Gauge and which is a Counter**.
# This change only touches the alert engine side: the backend side's name is referenced by live PromQL
# and `rules/robot-alerts.yml`, changing it would be cross-order rework (order 32 forbidden item 5).
# After adding `_gauge`, the scrape side and panel authors can tell at a glance "this can go up and down,
# forbid rate() on it", and the difference between the two names also becomes explicit and checkable.
#
# Criterion source (last_seen within threshold) see the ticker at `alert_engine/app/main.py:252`;
# the numerator/denominator caliber (online / fixed 100) is refreshed by the ticker every second.
robots_online = Gauge(
    "robot_alert_robots_online_gauge",
    "Number of robots currently judged online by the ticker line (denominator fixed at 100)",
    registry=registry,
)
#: Number of alerts that **failed to be written to the DB** (`increase(...[5m]) > 0` triggers).
#:
#: Why this one must be instrumented separately: on this chain `robot_alert_emitted_total` only counts
#: "decided to emit after dedup", and `store.save()`'s insert exception previously **only went to logs**.
#: So "no DB record" and "alert never triggered" are completely isomorphic in monitoring --
#: the dashboard goes green as usual, but the audit reconciliation is short on data (G-4). With this,
#: the two become distinguishable: emitted rising while store_errors also rising => the alert was indeed
#: judged, just not persisted.
store_errors = Counter(
    "robot_alert_store_errors_total",
    "Number of alerts that failed to be written to the DB (>0 means 'alerts exist but not in DB', green dashboard is fake)",
    registry=registry,
)

#: Number of notifications **successfully delivered** (R-15 T1). Paired with `notify_failed` to compute failure rate.
#:
#: Why count successes too: with only a failure counter, seeing `notify_failed_total = 37` cannot tell
#: how serious it is -- is it 1% packet loss or 100% total loss? The denominator (denominator = sent +
#: failed) must exist, so `rate(notify_failed[5m]) / (rate(sent[5m]) + rate(notify_failed[5m]))` makes sense.
notify_sent = Counter(
    "robot_alert_notify_sent_total",
    "Number of alerts successfully delivered to the group bot (paired with notify_failed_total for delivery failure rate)",
    ["kind"],
    registry=registry,
)

#: Number of **failed** notification deliveries, broken down by `reason` (the core deliverable of R-15 / GAP-3).
#:
#: Why this must exist: notification failures previously **only went to logs**. When the webhook is
#: misconfigured / DingTalk-WeCom is rate-limited, the alert packet is persisted and broadcast, but the
#: on-duty staff never receives it in chat -- while the Prometheus side knows nothing (the existing
#: `alerts_emitted` only counts "decided to emit", unrelated to delivery success/failure).
#: So "notification silently lost" is completely isomorphic to "notification normal" in monitoring, and
#: the dashboard is forever green.
#:
#: `reason` values are a **closed set** (see `notifiers/webhook.py:NOTIFY_REASONS`):
#: `timeout` / `network` (worth retrying, check network), `http_error` / `business_error`
#: (mostly config errors, check token), `config` (not sent locally), `unknown` (catch-all, not masked).
#: The dimension is deliberately narrowed -- one label per distinct exception string would make the
#: Prometheus time-series count explode.
notify_failed = Counter(
    "robot_alert_notify_failed_total",
    "Number of failed alert-notification deliveries (by reason; >0 means 'alert in DB but nobody got it in chat')",
    ["reason"],
    registry=registry,
)

#: Number of Redis pubsub **disconnect/reconnect** events (R-14).
#:
#: Why this must exist (rather than just logging): `consume()`'s `pubsub.listen()` throws
#: `redis.exceptions.ConnectionError` on a Redis blip. Before R-14 such exceptions escaped the coroutine
#: directly and, via `asyncio.gather`, brought down the whole process; after the fix it is **swallowed
#: inside the coroutine** and goes through backoff reconnect -- the cost is that "the engine is repeatedly
#: blipping" degrades from "process crash" (visible via container restart) to **completely silent internal
#: state**: `/metrics` still 200, `up=1`, dashboard all green, but at this moment the streaming alert
#: consumption is in backoff waiting and is **genuinely not consuming**.
#: With this counter, `increase(robot_alert_redis_reconnect_total[5m]) > 0` can expose such "fake alive"
#: (same idea as store_errors in R-15: only counting "decided to do" but not "actually done" makes the
#: dashboard green fake).
#:
#: Only counts the number of **reconnect attempts initiated** (including those that recovered after this
#: reconnect), without success/failure split: a failure will immediately enter the next backoff round, so
#: "count keeps rising" itself is evidence that failure has not terminated, no second metric needed.
redis_reconnect = Counter(
    "robot_alert_redis_reconnect_total",
    "Number of reconnect attempts initiated by consume() due to Redis disconnection (rebuild pubsub and re-subscribe after backoff)",
    registry=registry,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming convention)
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = generate_latest(registry)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # Silent: avoid flooding logs on every scrape
        return


def start(port: int = 8001) -> ThreadingHTTPServer:
    """Start the metrics HTTP server on a background thread; return the server for shutdown."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104
    t = threading.Thread(target=server.serve_forever, name="metrics-http", daemon=True)
    t.start()
    log.info("alert_engine metrics on :%d/metrics", port)
    return server
