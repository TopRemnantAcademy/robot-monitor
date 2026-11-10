"""Regression for `assert_metric_naming`'s criteria (Order 34).

Why a separate file: this assertion is an **anti-example gate** -- it must be able
to go red, and must not go red falsely. The real-world case in Order 34 (CI build
`cnb-dr0-1k4pbtmr7-002`) was a **false red**: `robot_backend_batch_flushes_total`
carries a label, prometheus_client registers lazily, and with zero traffic there is
no sample line, so the probe treated "no series" as "metric does not exist" and
turned the whole e2e stage red. The fix was to split the criteria by label (see
`REQUIRED_BACKEND_METRICS` / `LABELED_BACKEND_METRICS` in `test_e2e_30k.py`).

This file only feeds fabricated /metrics text to the assertion function; it does not
bring up a stack and needs no Docker -- so it **always really runs** under a bare
`pytest` (testpaths includes `tests`), which is the only place this criterion can be
verified locally.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent / "test_e2e_30k.py"
_spec = importlib.util.spec_from_file_location("e2e_30k_probe", _MOD_PATH)
e2e = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(e2e)


# /metrics of a zero-traffic stack: unlabeled metrics are all present, while the
# labeled batch_flushes has no sample line yet. Structure matches the real backend's
# render() output (only rows relevant to this test are kept).
_ZERO_TRAFFIC = """\
# HELP robot_backend_queue_depth asyncio.Queue current backlog count
# TYPE robot_backend_queue_depth gauge
robot_backend_queue_depth 0.0
# HELP robot_backend_batch_flushes_total BatchWriter flush batch count
# TYPE robot_backend_batch_flushes_total counter
# HELP robot_backend_telemetry_written_total successfully written telemetry rows
# TYPE robot_backend_telemetry_written_total counter
robot_backend_telemetry_written_total 0.0
robot_backend_mqtt_received_total 0.0
robot_backend_ws_connections 0.0
robot_backend_history_latency_seconds_count 0.0
robot_backend_history_latency_seconds_sum 0.0
"""

# /metrics after load testing: batch_flushes now has {reason=...} sample lines.
_LOADED = _ZERO_TRAFFIC + 'robot_backend_batch_flushes_total{reason="count"} 80.0\n'


def _serve(monkeypatch, text: str) -> None:
    monkeypatch.setattr(e2e, "http_get", lambda url, timeout=5: text)


def test_zero_traffic_allows_unemitted_labeled_counter(monkeypatch):
    """Zero traffic + allow_unemitted=True: absence of labeled metric is expected, must not go red."""
    _serve(monkeypatch, _ZERO_TRAFFIC)
    e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)


def test_zero_traffic_strict_mode_still_reds(monkeypatch):
    """Zero traffic + default (strict) criterion: absence of labeled metric must go red -- the exemption is not unconditional."""
    _serve(monkeypatch, _ZERO_TRAFFIC)
    with pytest.raises(AssertionError, match="robot_backend_batch_flushes_total"):
        e2e.assert_metric_naming("http://x/metrics", "robot_")


def test_loaded_stack_requires_full_set(monkeypatch):
    """With traffic: even under the strictest criterion, the full set must be present."""
    _serve(monkeypatch, _LOADED)
    e2e.assert_metric_naming("http://x/metrics", "robot_")


def test_legacy_name_leak_reds_even_with_exemption(monkeypatch):
    """Anti-example self-check: once a legacy-prefix metric leaks, it must go red even with the exemption -- the gate must not become permanently green."""
    _serve(monkeypatch, _LOADED + "robot_monitor_batch_queue_size 1.0\n")
    with pytest.raises(AssertionError, match="robot_monitor_batch_queue_size"):
        e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)


def test_unlabeled_missing_reds_even_with_exemption(monkeypatch):
    """Anti-example self-check: missing unlabeled canonical name is not covered by the exemption, must go red."""
    _serve(monkeypatch, _LOADED.replace("robot_backend_queue_depth 0.0\n", ""))
    with pytest.raises(AssertionError, match="robot_backend_queue_depth"):
        e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)
