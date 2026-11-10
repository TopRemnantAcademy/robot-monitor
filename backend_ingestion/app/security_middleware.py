"""CORS + optional rate limiting (R-17, source docs/GAP-SCAN-2026-10-13.md GAP-4).

## Why this file is needed

Grepping `main.py` for `cors|CORSMiddleware|rate.?limit|throttle|slowapi|add_middleware`
returned **0 hits** (R-17 SS1 evidence). The only existing protection was JWT
auth. Two real risk classes:

1. **Missing CORS**: when the frontend and backend are **cross-origin** (frontend
   on its own domain/CDN, or local dev `:5173` hitting `:8000`), the browser
   blocks the REST and WS handshake. The failure happens browser-side and the
   server log stays clean -- the highest-cost-to-debug kind of problem.
2. **No rate limiting**: the external-facing interface has no rate limit. An
   intranet trusted topology is acceptable, but once exposed (including
   `/auth/jwt/login`) there is no basic brute-force / API-abuse protection.

## Safe defaults: off / minimal exposure (R-17 SS2 SS6 hard constraints)

- **CORS off by default**: `CORS_ALLOW_ORIGINS` empty -> **do not mount**
  `CORSMiddleware`, so no `Access-Control-Allow-Origin` appears in responses.
  Never default to `allow_origins=["*"]`: under the browser spec `*` combined
  with `allow_credentials=True` is disallowed, and if someone "to make it work"
  switches to echoing Origin + credentials, the CSRF/data-exposure surface goes
  from "same-origin" to "any site".
  Only an explicit `CORS_ALLOW_ORIGINS=*` opens all origins (separate code branch
  + risk warning log).
- **Rate limiting off by default**: `RATE_LIMIT_ENABLED=false`. Default means
  "no rate-limiting logic at all"; existing e2e / probe / monitoring request
  cadence is byte-for-byte unchanged (R-17 SS6 hard gate).

## Why not slowapi for rate limiting

R-17 SS3 requires preferring stdlib or the existing stack and evaluating new
dependency size/maintenance cost. `slowapi` is a `limits` + `decorator` chain,
and its main usage is **per-route decorator** -- that would change route handlers
(R-17 SS3 explicitly "do not change route handlers"). Here we use ASGI middleware
+ an in-process fixed-window counter: **zero new dependency**, and naturally
"only add middleware, never touch routes".

## Rate-limiting exempt surface (R-17 SS6 hard gate, not one may be dropped)

- `/healthz`, `/metrics`: relied on at second-level frequency by CI/e2e/container
  orchestration/Prometheus. Limiting them = probe declares dead and restarts,
  monitoring silently fails (order 15 already whitelists these two as auth-exempt;
  here they must be rate-exempt too).
- `/ws/robots`: WS is a long connection with a one-time handshake; rate limiting
  is meaningless and would interrupt the frontend real-time stream.
- Internal write path: `/internal/*` prefix (reserved internal-write convention,
  see `INTERNAL_PATH_PREFIX`).

## Over-limit response format

`429 Too Many Requests` + `{"detail": "rate limit exceeded"}` -- same shape as the
existing error responses (FastAPI default `{"detail": ...}` / `main.py`'s 503
`{"detail": ...}`). Includes a `Retry-After` header in seconds (RFC 9110 SS10.2.3).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("security_middleware")

# Internal write-path prefix. R-17 SS4 T2 requires "must exclude internal write
# paths" -- these are service-to-service calls (reliable, high-frequency, not
# user-driven); limiting them loses data rather than preventing abuse.
# This repo has no /internal/* routes yet; reserve by prefix here: once an
# internal-write endpoint is mounted later, there is no need to come back and
# edit the rate-limit whitelist (avoids the "new endpoint got rate-limited" kind
# of problem found only after launch).
INTERNAL_PATH_PREFIX = "/internal/"

# Paths always exempt from rate limiting (exact match). See module header
# "rate-limiting exempt surface".
EXEMPT_PATHS = frozenset({"/healthz", "/metrics"})


def _parse_origins(raw: str) -> list[str]:
    """`CORS_ALLOW_ORIGINS` comma-separated -> list after stripping whitespace and
    dropping empty items.

    The only special value is `*` (explicitly allow all origins). Beyond that,
    **do not parse, do not infer** -- the user writes what they mean, to avoid
    "helpfully" completing `example.com` to `https://example.com` (a wrong
    completion is more dangerous: the user thinks they configured A but actually
    allowed B).
    """
    return [item.strip() for item in raw.split(",") if item.strip()]


def install_cors(app: FastAPI, raw_origins: str) -> list[str]:
    """Mount CORS middleware per `raw_origins`. Empty -> not mounted, returns [].

    Returns the actually-effective origins (for caller logging/self-check); if we
    did not return it, the caller could not distinguish "config empty so not
    mounted" from "code forgot to mount".
    """
    origins = _parse_origins(raw_origins)
    if not origins:
        # Default path (unconfigured) -- deliberately **do not mount** middleware,
        # rather than mounting one with allow_origins=[]. An empty-list middleware
        # returns 400 on preflight (no Allow-Origin), which is another form of
        # "rejection" but still intercepts OPTIONS and changes the error shape.
        # Not mounting = byte-identical to pre-change.
        log.info("CORS disabled (CORS_ALLOW_ORIGINS empty) — same-origin only")
        return []

    wildcard = origins == ["*"]
    if wildcard:
        # Explicit `*`: the user clearly asked to allow all origins. A risk note
        # is mandatory -- this is exactly the path R-17 SS4 T1 wants "flagged in
        # comments/docs as risky". Warning-level log makes it visible in prod logs.
        log.warning(
            "CORS: CORS_ALLOW_ORIGINS='*' — allowing ALL origins (explicitly configured). "
            "This widens CSRF/data-exposure surface; restrict to known frontend origins in production."
        )
    else:
        log.info("CORS enabled for %d origin(s): %s", len(origins), origins)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # Minimal usable set (R-17 SS4 T1):
        # - credentials=True: the frontend uses `Authorization: Bearer <JWT>`
        #   (order 15). This is not a cookie credential, but the browser still
        #   requires Allow-Credentials on cross-origin XHR (a cross-origin request
        #   carrying an Authorization header goes through preflight, and a missing
        #   response header would block it).
        #   BECAUSE of this, **even more** must we not default `*`: `*` +
        #   credentials is both spec-prohibited and the classic "any site can hit
        #   the backend with the user's credentials" accident shape.
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    return origins


# ---------------------------------------------------------------------------
# Optional rate limiting (off by default)
# ---------------------------------------------------------------------------

class RateLimitMiddleware:
    """In-process fixed-window rate limiting (ASGI layer, zero dependency).

    [Why fixed-window rather than sliding-window / token-bucket]
    This is the **lowest tier of abuse protection** (R-17 SS1: "acceptable" under
    an intranet trusted topology, a backstop when exposed), not a precise quota.
    Fixed-window's boundary spike (up to 2x quota at window switch) is acceptable
    here, and it buys: no lock, no background task, no memory growth (each key
    stores only two ints).

    [Why count by client IP]
    Auth happens in the **route dependency** (`current_active_user`); the
    middleware runs before it and cannot see the user identity. IP is the closest
    "abuser" identifier this layer can get. The cost is multiple users behind a
    NAT sharing a quota -- consistent with the "off by default" positioning:
    whoever enables it knows they are doing coarse-grained protection.

    [Bounded memory]
    `_hits` stores one entry per (window, key). On window switch the key's old
    record is overwritten, but a **never-seen key is never reclaimed** -- under
    long uptime with low-frequency IP scans, the dict grows slowly. The trade-off
    here: the middleware instance is process-level, and the IP count in a single
    process is limited (factory intranet); if truly exposed, rate limiting should
    sink to the gateway (R-17 SS3 explicitly "do not introduce a heavyweight
    gateway" is this ticket's boundary; the gateway plan is out of scope). To
    control risk, `_check`'s window flip clears stale entries.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        exempt_paths: frozenset[str] = EXEMPT_PATHS,
        internal_prefix: str = INTERNAL_PATH_PREFIX,
    ) -> None:
        self.app = app
        self.per_minute = per_minute
        self.exempt_paths = exempt_paths
        self.internal_prefix = internal_prefix
        self._window = 60.0
        self._window_start = 0.0
        self._hits: dict[str, int] = defaultdict(int)

    def _client_key(self, scope: Scope) -> str:
        """Client identifier = direct address.

        Deliberately **distrust** `X-Forwarded-For`: that header is freely
        forgeable by the client, so using it as the rate-limit key means an
        attacker rotates one header value to bypass. Only when explicitly deployed
        behind a trusted proxy (and knowing the proxy overwrites that header)
        should XFF be enabled -- that belongs to gateway registration, outside
        this ticket's boundary.
        """
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_exempt(self, path: str, scope_type: str) -> bool:
        # WS handshake is not rate-limited: a long connection handshakes once;
        # limiting it / causing frontend reconnect storms is worse.
        # (the scope_type == "websocket" branch returns early in __call__; this is
        # only a defensive check.)
        if scope_type == "websocket":
            return True
        if path in self.exempt_paths:
            return True
        if path.startswith(self.internal_prefix):
            return True
        return False

    def _check(self, key: str, now: float) -> bool:
        """Return True=allow, False=over-limit. Also performs the window flip and
        stale-entry sweep."""
        if now - self._window_start >= self._window:
            # Window page turn: open a new window. The old window's counts are no
            # longer meaningful, so swap in a new dict -- this is also the memory
            # reclaim point (the "stale entries" from the __init__ comment are
            # cleared here).
            self._window_start = now
            self._hits = defaultdict(int)
        self._hits[key] += 1
        return self._hits[key] <= self.per_minute

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan is ASGI's third scope type (uvicorn uses it for
        # startup/shutdown); it must pass -- blocking it means the process won't
        # start.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_exempt(path, scope["type"]):
            await self.app(scope, receive, send)
            return

        now = time.monotonic()
        if self._check(self._client_key(scope), now):
            await self.app(scope, receive, send)
            return

        # Over limit. Response body shape matches existing error responses
        # ({"detail": ...}); besides the frontend 401 interceptor, just hang a 429
        # handler -- no new structure to learn.
        retry_after = max(1, int(self._window - (now - self._window_start)) + 1)
        log.warning("rate limit exceeded: path=%s", path)
        response = JSONResponse(
            {"detail": "rate limit exceeded"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


def install_rate_limit(app: FastAPI, *, enabled: bool, per_minute: int) -> bool:
    """Mount rate-limit middleware per env. Returns whether it actually enabled
    (for caller logging)."""
    if not enabled:
        log.info("rate limit disabled (RATE_LIMIT_ENABLED=false) — no throttling")
        return False
    if per_minute <= 0:
        # A non-positive threshold equals "reject all", which is a config error
        # not a user intent. Refuse to enable and warn; never silently become
        # "all requests 429" (that is far more dangerous than no limit).
        log.error(
            "RATE_LIMIT_PER_MINUTE=%d is invalid (must be > 0) — rate limit NOT enabled",
            per_minute,
        )
        return False
    app.add_middleware(RateLimitMiddleware, per_minute=per_minute)
    log.info(
        "rate limit enabled: %d req/min per client IP; exempt=%s, internal_prefix=%s",
        per_minute,
        sorted(EXEMPT_PATHS),
        INTERNAL_PATH_PREFIX,
    )
    return True
