"""Order 16 observability contract tests (T1b / T3 / T4).

This file locks down the acceptance criteria from `orders/16-dispatch.md` section 2 that can be
verified **without PG/Redis/MQTT**. We lock "acceptance criteria", not implementation details:
changing the implementation should not turn red as long as the semantics are unchanged.

Run (must be run separately; rationale in pytest.ini and the conftest.py header):
    pytest backend_ingestion/tests/

Coverage:
  T1b  /metrics is unauthenticated; after ISSUE #72 there is **only** the robot_backend_* naming
       (no robot_monitor_*; the dual-track duplicate metrics were deleted); the queue Gauge tracks
       the real queue (acceptance requires "not a constant 0")
  T1b  Forbidden to label by robot_id (high cardinality) -- assert via metric names, not by hand
  T3   OTel is a no-op when unconfigured; _traces_url normalization; degrades when package missing
  T4   Logs are single-line JSON, parseable by json.loads and containing ts/level/logger/msg
  T4   Exception traceback is also within a single line (must not be torn into multiple lines by
       newlines, otherwise the collector misjudges log boundaries)
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import metrics, telemetry_tracing
from app.logging_json import JsonFormatter, configure_json_logging
from app.main import app

# ISSUE #72: the single canonical naming. robot_backend_* is referenced by production PromQL and
# scrape targets, so it must stay; the ws_connections / history_latency_seconds added by Order 16
# were renamed and merged in.
REQUIRED_BACKEND_METRICS = (
    "robot_backend_telemetry_written_total",
    "robot_backend_batch_flushes_total",
    "robot_backend_queue_depth",
    "robot_backend_mqtt_received_total",
    "robot_backend_mqtt_dropped_total",
    "robot_backend_ws_connections",
    "robot_backend_history_latency_seconds",
)

# After the dual-track naming unification (ISSUE #72), these prefixes must **never** reappear in
# /metrics. We assert on the prefix as a whole rather than name-by-name: to prevent someone later
# "handily" creating another robot_monitor_*.
FORBIDDEN_MONITOR_PREFIX = "robot_monitor_"


@pytest.fixture
def client():
    """TestClient does not run lifespan (no MQTT/Redis/PG), but routes and dependency resolution
    are fully real."""
    return TestClient(app, raise_server_exceptions=False)


class TestMetricsEndpoint:
    """/metrics must be unauthenticated (Prometheus pull), and missing any metric is an
    acceptance failure."""

    def test_metrics_is_unauthenticated(self, client):
        """Order 16 forbidden item 2: adding auth to /metrics = monitoring silently fails.

        Prometheus's scrape config has no Bearer token; adding auth makes the scrape return 401,
        showing up as `up{job="robot_backend"}=0` rather than an error -- the most easily
        overlooked failure mode.
        """
        assert client.get("/metrics").status_code == 200

    def test_all_backend_metrics_exposed(self, client):
        """ISSUE #72 acceptance: all 7 metrics under the single robot_backend_* naming are exposed."""
        body = client.get("/metrics").text
        missing = [n for n in REQUIRED_BACKEND_METRICS if n not in body]
        assert not missing, f"missing metrics: {missing}"

    def test_legacy_metrics_preserved(self, client):
        """The 5 robot_backend_* metrics that production PromQL/scrape targets depend on must
        still exist (deletion strictly forbidden)."""
        body = client.get("/metrics").text
        missing = [n for n in REQUIRED_BACKEND_METRICS[:5] if n not in body]
        assert not missing, f"existing metrics mistakenly deleted: {missing}"

    def test_no_dual_track_monitor_prefix(self, client):
        """ISSUE #72: after the dual-track naming unification, no robot_monitor_* metric may
        reappear in /metrics.

        What we assert is the **metric name** (at line start or as a label name), not comments --
        mentioning the historical naming in a comment is allowed (it is migration history); only a
        timeseries actually registered into the registry counts as a violation.
        """
        body = client.get("/metrics").text
        leaked = [
            line for line in body.splitlines()
            if line.startswith(FORBIDDEN_MONITOR_PREFIX)
        ]
        assert not leaked, f"leftover robot_monitor_* metrics: {leaked}"

    def test_flush_counter_has_reason_label(self, client):
        """Labeling by trigger reason is the only evidence that "dual triggers fire as designed".

        Note that a Counter's label series is **lazily registered**: `robot_backend_batch_flushes_total{...}`
        only appears after the first `labels(reason=...).inc()` (official Prometheus behavior, not
        a defect of this implementation). So here we must first **produce one observation**, then
        assert the label exists -- directly scraping an empty registry yields 0 lines and would
        falsely report "no label".
        """
        from app import metrics

        metrics.observe_flush("count", 7)
        body = client.get("/metrics").text
        assert 'robot_backend_batch_flushes_total{reason="count"}' in body

    def test_observe_flush_rejects_unknown_reason(self, client):
        """A dirty reason is normalized to 'unknown' rather than silently dropped -- makes typos
        visible on the panel."""
        from app import metrics

        metrics.observe_flush("typo-value", 1)
        assert 'robot_backend_batch_flushes_total{reason="unknown"}' in client.get(
            "/metrics"
        ).text

    def test_string_robot_id_never_used_as_label(self, client):
        """Order 16 forbidden item 1: labeling by robot_id is forbidden (100 robots of high
        cardinality would crush the TSDB).

        We use `humanoid-` as a probe for "is robot_id written into a label": robot_id prefixes are
        always humanoid- (AGENTS.md section 4), so its presence in a label position is a violation.
        """
        body = client.get("/metrics").text
        for line in body.splitlines():
            if line.startswith("#") or "{" not in line:
                continue
            label_part = line.split("{", 1)[1].split("}", 1)[0]
            assert "humanoid-" not in label_part, f"robot_id leaked into a label: {line}"

    def test_queue_gauge_tracks_real_depth(self, client):
        """Acceptance criterion: queue_size must have real fluctuation, not a constant 0.

        We directly push elements into writer.queue and then scrape -- if the Gauge isn't refreshed
        in /metrics (under the pull model nothing sets it for us), this would get 0.
        """
        from app.main import writer

        def read_gauge() -> float:
            for line in client.get("/metrics").text.splitlines():
                if line.startswith("robot_backend_queue_depth "):
                    return float(line.split()[1])
            raise AssertionError("robot_backend_queue_depth not exposed")

        before = read_gauge()
        for _ in range(5):
            writer.queue.put_nowait(object())  # content irrelevant; only verifying Gauge refresh
        after = read_gauge()
        assert after == before + 5, f"Gauge did not track the queue: {before} -> {after}"


class TestOtelDegradability:
    """Order 16 section 1 T3 + section 2: unconfigured must be a no-op, and must **never** cause
    startup to fail."""

    def test_empty_endpoint_is_noop(self):
        assert telemetry_tracing.configure_tracing("") is False

    def test_traces_url_appends_path_once(self):
        assert telemetry_tracing._traces_url("http://c:4318") == "http://c:4318/v1/traces"
        assert (
            telemetry_tracing._traces_url("http://c:4318/v1/traces")
            == "http://c:4318/v1/traces"
        )
        assert telemetry_tracing._traces_url("http://c:4318/") == "http://c:4318/v1/traces"

    def test_import_main_needs_no_otel_packages(self):
        """All OTel imports live inside function bodies -- zero dependency at module top level.

        This is the structural guarantee of "degradability": even if the environment has no
        opentelemetry-*, `import app.main` must still succeed (this file itself runs in a CI
        without OTel).
        """
        import app.main  # noqa: F401 -- reaching this line IS the assertion


class TestJsonLogging:
    """Order 16 T4: single-line JSON, parseable by jq."""

    def test_record_is_single_line_json(self):
        fmt = JsonFormatter()
        rec = logging.LogRecord(
            "demo", logging.INFO, __file__, 1, "hello %s", ("world",), None
        )
        out = fmt.format(rec)
        assert "\n" not in out, "single-line contract broken: collector would misjudge log boundaries"
        payload = json.loads(out)  # equivalent assertion to "parseable by jq"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "demo"
        assert payload["msg"] == "hello world"
        assert payload["ts"].endswith("Z")

    def test_traceback_stays_single_line(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = logging.LogRecord(
                "demo", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        out = fmt.format(rec)
        assert "\n" not in out, "exception traceback tore the log into multiple lines"
        assert "ValueError: boom" in json.loads(out)["exc"]

    def test_extra_robot_id_is_preserved(self):
        """Structured fields (e.g. robot_id) must appear at the JSON top level, for the collector
        to index."""
        fmt = JsonFormatter()
        rec = logging.LogRecord("demo", logging.INFO, __file__, 1, "x", (), None)
        rec.robot_id = "humanoid-042"
        assert json.loads(fmt.format(rec))["robot_id"] == "humanoid-042"

    def test_configure_is_idempotent(self):
        """Repeated configuration must not stack handlers (stacking would print the same line N
        times)."""
        configure_json_logging()
        n1 = len(logging.getLogger().handlers)
        configure_json_logging()
        n2 = len(logging.getLogger().handlers)
        assert n1 == n2 == 1
