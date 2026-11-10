"""R-17 -- env-driven CORS + optional rate-limiting regression tests.

What is pinned here is the R-17 section 5 acceptance criteria (V1~V3, V5), not
implementation details: changing the implementation should not turn red as long as
the semantics are unchanged.

Run (must be run separately; rationale in pytest.ini and the conftest.py header):
    python -m pytest backend_ingestion/tests/test_security_middleware.py -v

No PG / Redis / MQTT / MQTT broker needed:
- CORS / rate-limiting are both ASGI middleware, coverable with TestClient (no lifespan,
  no dependency connections).
- The app under test uses `app.main.app` (the **real** assembly order: middleware is
  mounted before include_router), instead of hand-building a FastAPI -- the latter
  would not prove "it is really mounted in production".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from app.main import app as real_app
from app.security_middleware import (
    EXEMPT_PATHS,
    INTERNAL_PATH_PREFIX,
    RateLimitMiddleware,
    install_cors,
    install_rate_limit,
)


def _app_with_routes() -> FastAPI:
    """A minimal FastAPI: one normal route + two critical paths (/healthz, /metrics).

    The critical paths use fake implementations here (no DB/Redis connection) -- what
    this file checks is **whether the middleware lets them through**, not the business
    logic of those two endpoints (that is covered by test_observability.py).
    """
    a = FastAPI()

    @a.get("/robots")
    async def _robots():  # pragma: no cover - only used by the route table
        return {"robots": []}

    @a.get("/healthz")
    async def _healthz():  # pragma: no cover
        return {"status": "ok"}

    @a.get("/metrics", response_class=PlainTextResponse)
    async def _metrics():  # pragma: no cover
        return "robot_backend_queue_depth 0\n"

    return a


# ------------------------------------------------------------------ V1: CORS safe by default

class TestCorsDefaultSafe:
    """V1: when `CORS_ALLOW_ORIGINS` is not configured -> response has **no**
    `Access-Control-Allow-Origin`."""

    def test_no_cors_middleware_when_empty(self):
        """Empty config -> no middleware mounted (not "mount an empty list"); behavior is
        byte-for-byte identical to before the change."""
        a = _app_with_routes()
        assert install_cors(a, "") == []
        # Browser side: a cross-origin GET response must not carry any CORS header.
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    def test_whitespace_only_is_treated_as_empty(self):
        """Whitespace/commas only also counts as "not configured" -- a stray extra comma
        must not accidentally enable CORS."""
        assert install_cors(_app_with_routes(), "  , ,, ") == []

    def test_real_app_default_has_no_cors_header(self):
        """The production app (the default path with no CORS_ALLOW_ORIGINS set) is equally
        strict.

        This one watches the **assembly** in app.main: even if install_cors is correct,
        if main.py forgets to call it or passes `*`, this assertion will turn red.
        """
        # conftest only injects JWT_SECRET, not CORS_ALLOW_ORIGINS -> default off expected.
        with TestClient(real_app, raise_server_exceptions=False) as c:
            r = c.get("/healthz", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ------------------------------------------------------------------ V2: CORS configurable

class TestCorsConfigurable:
    """V2: with a configured origin -> preflight `OPTIONS` returns the correct
    Allow-Origin / Allow-Methods."""

    def test_preflight_returns_configured_origin(self):
        a = _app_with_routes()
        assert install_cors(a, "http://localhost:5173") == ["http://localhost:5173"]
        with TestClient(a) as c:
            r = c.options(
                "/robots",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization",
                },
            )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "GET" in r.headers["access-control-allow-methods"]
        assert r.headers["access-control-allow-credentials"] == "true"

    def test_unlisted_origin_is_not_allowed(self):
        """An Origin outside the whitelist must not be echoed back -- only the configured
        one may be echoed."""
        a = _app_with_routes()
        install_cors(a, "http://localhost:5173")
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://evil.example"})
        assert r.headers.get("access-control-allow-origin") != "http://evil.example"

    def test_multiple_origins_parsed(self):
        """Comma-separated + whitespace-trimmed: both must enter the whitelist."""
        a = _app_with_routes()
        origins = install_cors(
            a, " http://localhost:5173 , https://board.example.com "
        )
        assert origins == ["http://localhost:5173", "https://board.example.com"]

    def test_explicit_wildcard_path_allows_any_origin(self):
        """Only an explicit `*` allows any origin (the **only** path that permits arbitrary
        origins)."""
        a = _app_with_routes()
        assert install_cors(a, "*") == ["*"]
        with TestClient(a) as c:
            r = c.get("/robots", headers={"Origin": "http://anything.example"})
        assert r.headers["access-control-allow-origin"] == "*"


# ------------------------------------------------------------------ V3: rate limit off by default + exempt paths

class TestRateLimitOffByDefault:
    """V3 first half: by default (env off) no rate-limit middleware is mounted, no path
    ever returns 429."""

    def test_disabled_returns_false_and_never_429(self):
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=False, per_minute=1) is False
        with TestClient(a) as c:
            # Threshold 1 yet 5 hits in a row all 200 -- proves the middleware is absent.
            codes = [c.get("/robots").status_code for _ in range(5)]
        assert codes == [200] * 5

    def test_invalid_threshold_does_not_enable(self):
        """A threshold <= 0 is a config error: refuse to enable, never silently turn into
        "every request 429"."""
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=True, per_minute=0) is False
        with TestClient(a) as c:
            assert c.get("/robots").status_code == 200


class TestRateLimitExemptPaths:
    """V3 second half (**critical**): once rate limiting is on, /healthz, /metrics,
    /internal/* are never limited."""

    @pytest.mark.parametrize("path", sorted(EXEMPT_PATHS))
    def test_exempt_paths_never_429(self, path: str):
        a = _app_with_routes()
        assert install_rate_limit(a, enabled=True, per_minute=2) is True
        with TestClient(a) as c:
            # Threshold 2, hit 10 times: an ordinary path would be 429 long ago; an exempt
            # path must stay all 200.
            codes = [c.get(path).status_code for _ in range(10)]
        assert codes == [200] * 10, f"{path} was rate-limited (R-17 section 6 hard gate)"

    def test_internal_prefix_exempt(self):
        """Internal write paths (/internal/*) are exempt -- limiting them would drop data
        rather than prevent abuse."""
        a = _app_with_routes()

        @a.post(INTERNAL_PATH_PREFIX + "ingest")
        async def _ingest():  # pragma: no cover
            return {"ok": True}

        install_rate_limit(a, enabled=True, per_minute=2)
        with TestClient(a) as c:
            codes = [c.post(INTERNAL_PATH_PREFIX + "ingest").status_code for _ in range(10)]
        assert codes == [200] * 10


class TestRateLimitEnforced:
    """Once rate limiting is on, **external REST** over the limit returns 429 +
    `{"detail": ...}`."""

    def test_exceeding_returns_429_with_detail(self):
        a = _app_with_routes()
        install_rate_limit(a, enabled=True, per_minute=3)
        with TestClient(a) as c:
            codes = [c.get("/robots").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200]
        assert codes[3:] == [429, 429]
        with TestClient(a) as c:
            c.get("/robots")  # not counted; just ensures the window state read below is consistent
        # Verify the response body separately: rebuild the app to avoid window-count interference.
        b = _app_with_routes()
        install_rate_limit(b, enabled=True, per_minute=1)
        with TestClient(b) as c:
            c.get("/robots")
            r = c.get("/robots")
        assert r.status_code == 429
        assert r.json() == {"detail": "rate limit exceeded"}
        assert int(r.headers["retry-after"]) >= 1

    def test_ws_scope_is_not_throttled(self):
        """A WS handshake is not external REST: a long-lived connection handshakes once;
        limiting it would only make the frontend's reconnect storm worse.

        Call the middleware directly and assert it passes the scope through to the
        downstream (no real handshake -- that needs the full WS route + auth, covered by
        test_ws_*).
        """
        mw = RateLimitMiddleware(app=None, per_minute=1)
        assert mw._is_exempt("/ws/robots", "websocket") is True


# ------------------------------------------------------------------ V5: existing contract unchanged

class TestRealAppContractUnchanged:
    """V5: after mounting the middleware, existing endpoint contracts and auth behavior
    are unchanged (spot-check the default path)."""

    def test_healthz_shape_unchanged(self):
        with TestClient(real_app, raise_server_exceptions=False) as c:
            r = c.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        # The existing healthz fields (R-03 section 4 contract; field set in
        # schemas/api.py:HealthResponse).
        for field in ("status", "db", "written", "flushes", "rows_written", "rows_submitted"):
            assert field in body, f"/healthz missing field {field}"

    def test_metrics_unauthenticated_unchanged(self):
        """The auth-exempt whitelist is unchanged: /metrics is still 200 (Order 15)."""
        with TestClient(real_app, raise_server_exceptions=False) as c:
            assert c.get("/metrics").status_code == 200

    def test_robots_still_requires_auth(self):
        """Auth behavior unchanged: /robots without a token is still 401 (Order 15)."""
        with TestClient(real_app, raise_server_exceptions=False) as c:
            assert c.get("/robots").status_code == 401
