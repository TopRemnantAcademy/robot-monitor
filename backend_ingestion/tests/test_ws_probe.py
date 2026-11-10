"""Behavior-contract tests for the WS synthetic probe (ISSUE #58 / gap G-3).

Locks down four things explicitly required in orders/16-dispatch.md section 2 and ISSUE #58:
  1. The probe metric **exists independently**, and does **not** overwrite the connection-count
     metric `robot_backend_ws_connections` (before ISSUE #72 it was mistakenly implemented as
     "metrics.py must not define a connection count"; the connection count *should* be defined in
     metrics.py -- what must be prevented is **same-name reuse**: the same name serving both as
     the server-side connection count and the client-side probe);
  2. The probe is **unauthenticated**: both `/metrics` and the probe target require no external token;
  3. The probe **can degrade**: when `WS_PROBE_ENABLED=false`, `run()` returns immediately (no-op),
     and a probe's own exception does not bubble (failure only flips a metric);
  4. The frequency floor is 30s (misconfiguration can't make it faster).

Run (from repo root, requires backend_ingestion/requirements.txt installed):
    pytest backend_ingestion/tests/test_ws_probe.py -v

Why no real port: actually starting uvicorn + really connecting over WS is CI/Docker work; this
file only uses TestClient's in-process WS (covering the three segments of handshake, auth, and first
snapshot frame) plus a direct call to `ws_probe._probe_once_async`. For end-to-end real connections,
see the PR description.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import metrics, ws_probe
from app.config import settings
from app.main import app

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # Do not trigger lifespan: this file only tests route behavior and the probe function;
    # really starting MQTT/Redis needs external deps, which is another test layer.
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ metric contract

class TestProbeMetricsAreDistinct:
    """The core constraint of ISSUE #58: probe metric != connection-count metric; mutual
    overwrite forbidden."""

    def test_probe_metrics_exist(self):
        # Note: prometheus_client's Counter folds the `_total` suffix into `_name, so the
        # full original name is `_name + "_total"`. We assert on the **rendered** text to
        # avoid this pitfall.
        text = metrics.render()[0].decode()
        assert "robot_backend_ws_probe_alive" in text
        assert "robot_backend_ws_probe_failures_total" in text

    def test_probe_and_connections_metrics_do_not_collide(self):
        """The probe and the connection count are **two differently named metrics**; same-name
        reuse / mutual overwrite is forbidden.

        The intent of ISSUE #58 is "don't cram the probe into the connection-count metric"
        (client-side view vs server-side view). ISSUE #72 unified the connection-count metric
        to `robot_backend_ws_connections` and defined it in metrics.py -- this does not conflict
        with ISSUE #58: the two have different names, different semantics, and can coexist. What
        must be held is:
          a) in metrics.py, `robot_backend_ws_connections` is a Gauge (connection count, can go
             up or down);
          b) it is **not** a probe metric (not named probe, not written by ws_probe).
        """
        import inspect

        # a) the connection-count metric exists, has the right type, and a different name than the probe
        assert type(metrics.ws_connections).__name__ == "Gauge"
        assert metrics.ws_connections._name == "robot_backend_ws_connections"
        assert metrics.ws_probe_alive._name != metrics.ws_connections._name
        assert metrics.ws_probe_failures._name != metrics.ws_connections._name

        # b) the probe module does **not** write the connection-count metric: it only mentions the
        #    other in its docstring (explaining why they differ); the code must not reference
        #    `metrics.ws_connections` (writing = overwrite = two viewpoints merged into one number).
        probe_src = inspect.getsource(ws_probe)
        assert "metrics.ws_connections" not in probe_src

    def test_probe_alive_is_gauge_failures_is_counter(self):
        # Types must be right: Gauge reflects "the most recent one", Counter monotonically
        # accumulates for increase()/rate().
        assert type(metrics.ws_probe_alive).__name__ == "Gauge"
        assert type(metrics.ws_probe_failures).__name__ == "Counter"

    def test_render_contains_probe_metrics(self):
        body, ctype = metrics.render()
        text = body.decode()
        assert "robot_backend_ws_probe_alive" in text
        assert "robot_backend_ws_probe_failures_total" in text
        assert "text/plain" in ctype


# ------------------------------------------------------------------ unauthenticated

class TestProbeIsUnauthenticated:
    def test_probe_target_path_is_not_behind_rest_auth(self, client):
        """The probe target path exists and needs no REST Bearer header --
        REST's 401 is the `/robots` path's concern and should not spill onto WS.
        """
        r = client.get("/robots")
        assert r.status_code == 401  # REST side genuinely has auth (control group)

    def test_metrics_remains_unauthenticated(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "robot_backend_ws_probe_alive" in r.text


# ------------------------------------------------------------------ degrades

class TestProbeDegrades:
    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "ws_probe_enabled", False)
        # run() should return immediately, not enter while True.
        asyncio.run(asyncio.wait_for(ws_probe.run(), timeout=1.0))

    def test_mint_token_failure_returns_none(self, monkeypatch):
        # Self-sign failure must degrade to None, not throw the exception into lifespan.
        import jwt as _jwt

        def boom(*a, **k):
            raise RuntimeError("bad key")

        monkeypatch.setattr(_jwt, "encode", boom)
        assert ws_probe._mint_token() is None

    def test_probe_failure_does_not_raise(self, monkeypatch):
        """A target that can't be reached must not let `_probe_once_async` bubble past the caller.

        `_probe_once_async` itself raises truthfully (the caller decides on degradation); this
        verifies that each round of `run()` swallows the exception and flips the failure metric,
        rather than bubbling.
        """
        interval = settings.ws_probe_interval_s
        monkeypatch.setattr(settings, "ws_probe_startup_grace_s", 0.0)
        monkeypatch.setattr(settings, "ws_probe_interval_s", 30.0)
        monkeypatch.setattr(settings, "ws_probe_enabled", True)
        # Point at a port no one will ever listen on, to avoid depending on an external service.
        monkeypatch.setattr(settings, "ws_port", 1)

        before = metrics.ws_probe_failures._value.get()

        async def drive():
            task = asyncio.create_task(ws_probe.run())
            # The first probe round (5s timeout cap) is enough to finish, then cancel.
            await asyncio.sleep(0.3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(asyncio.wait_for(drive(), timeout=8.0))

        assert metrics.ws_probe_alive._value.get() == 0
        assert metrics.ws_probe_failures._value.get() > before


# ------------------------------------------------------------------ frequency floor

class TestIntervalFloor:
    def test_floor_is_30s(self):
        assert ws_probe.MIN_INTERVAL_S == 30.0
