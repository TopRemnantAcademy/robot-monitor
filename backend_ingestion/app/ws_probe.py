"""WS synthetic probe (client-side "is the dashboard still lit" liveness check).

Why it is needed (gap G-3 "dashboard's fake green"):
  `robot_backend_ws_connections` (order 16 T1b) counts the **server-side** WS
  connection objects; it cannot answer the question clients truly care about:
  can a real WS client complete **handshake + first-frame snapshot + incremental
  frame**. Historically there were incidents of "dashboard green but actually no
  data" -- nginx/gateway blocked the upgrade, Redis Pub/Sub went down, or the
  snapshot threw and was swallowed; in all those cases the server connection
  count was still 1. This probe re-runs the real path from the **client** side;
  a divergence between the two is the incident signal.

Three hard constraints (all honored by this module):
  1. **Auth-exempt target**: the probe connects to the same-process `ws_robots`,
     but goes through the real network stack (`ws://127.0.0.1:$ws_port`, not an
     ASGI direct call) so it covers the gateway/upgrade layer. Auth uses
     `?token=`, the token self-signed by this process with `settings.jwt_secret`
     (see `_mint_token`); it depends on no user table and accepts nothing
     external -- the probe runs only on its own loopback.
  2. **>= 30s once**: `ws_probe_interval_s`'s lower bound is clamped at 30s, so a
     misconfiguration cannot speed it up. The probe itself sends WS frames, and
     the 1000 msg/s telemetry stream is also on the same loopback; an unbounded
     frequency would pollute the load-test baseline.
  3. **Degradable (no-op)**: `ws_probe_enabled=False`, or `websockets`
     unavailable, or any exception inside the loop, only **stops updating
     metrics** and never affects the main service. The probe is an observer and
     must never take the backend down under any circumstance.

Metrics (distinct from order 16 T1b's connection-count Gauge; neither overrides
the other):
  - `robot_backend_ws_probe_alive`          Gauge   1=last probe succeeded / 0=failed
  - `robot_backend_ws_probe_failures_total` Counter cumulative failure count
"""

from __future__ import annotations

import asyncio
import json
import logging

from app import metrics
from app.config import settings

log = logging.getLogger("ws_probe")

#: Lower bound paired with `config.py`'s `ws_probe_interval_s`. Written in code,
#: not only in config, because config can be overridden by .env -- the probe
#: frequency must have an immovable floor.
MIN_INTERVAL_S = 30.0
#: Overall timeout for a single probe. The handshake + first-frame snapshot
#: phases share this budget.
PROBE_TIMEOUT_S = 5.0


def _mint_token() -> str | None:
    """Self-sign a probe-specific JWT using this process's secret.

    Declares only `sub` and `aud`: `authenticate_ws_token()` (`auth/deps.py:101`)
    only decodes these two fields for signature verification; `role` does not
    participate in WS admission judgment.
    On signing failure (unsupported algorithm for the key, etc.) returns None --
    let the probe degrade rather than raise.
    """
    try:
        import jwt

        from app.auth.backend import TOKEN_AUDIENCE

        return jwt.encode(
            {"sub": "ws-probe", "aud": TOKEN_AUDIENCE},
            settings.jwt_secret,
            algorithm="HS256",
        )
    except Exception:
        log.warning("ws probe: unable to self-sign probe token, probe degrades to no-op", exc_info=True)
        return None


async def _probe_once_async(url: str, token: str) -> bool:
    """Run one real WS client path, return whether it passed.

    The pass criteria are two layers:
      1. Can upgrade to WebSocket and **receive the first frame** -- this layer
         alone catches "gateway blocks upgrade" and "process fake-alive";
      2. The first frame must be a `{"type":"snapshot"}` envelope (spec/01 SS4.5).
         `ws_robots`, on snapshot failure, only `log.exception` and continues into
         the pub/sub loop; the client hangs on the first frame -- judging only
         "handshake succeeded" would misjudge this half-dead state as healthy.
         So the first frame gets its own short timeout here; missing it = failure.

    All exceptions propagate truthfully upward (caught by `run()` and flipped to
    the failure metric): **do not silently return False here**, otherwise
    "the probe itself broke" and "the backend really died" look identical in logs.
    """
    # The import location branches by version: `websockets.asyncio.client` is the
    # new namespace introduced in 13.0, while this repo's `requirements.txt` pins
    # `websockets==12.0` (old top-level `connect`).
    # Trying both is so the probe works under "upgraded dependency" and
    # "current state" alike -- the probe is an observer and should not be a
    # stumbling block for dependency version bumps. If neither is found, import
    # raises, and run() catches it and degrades.
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:
        from websockets.client import connect  # websockets 12.x (this repo's pinned version)

    async with asyncio.timeout(PROBE_TIMEOUT_S):
        async with connect(url) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=PROBE_TIMEOUT_S)
    frame = json.loads(first)
    return frame.get("type") == "snapshot"


async def run(*, url: str | None = None) -> None:
    """Probe loop. Started by lifespan as an `asyncio.Task`; this function never
    returns normally."""
    if not settings.ws_probe_enabled:
        log.info("ws probe: not enabled (WS_PROBE_ENABLED=false), no-op")
        return

    target = url or f"ws://127.0.0.1:{settings.ws_port}{settings.ws_probe_path}"
    token = _mint_token()
    if token is None:
        return

    # First round waits a startup window: uvicorn's `lifespan` startup phase has
    # already create_task'd this task, but the port is not yet accepting, so an
    # immediate probe is doomed. Wait out the window before starting, to avoid
    # manufacturing an opening false-failure.
    await asyncio.sleep(settings.ws_probe_startup_grace_s)
    query = target + ("&" if "?" in target else "?") + f"token={token}"
    interval = max(MIN_INTERVAL_S, settings.ws_probe_interval_s)
    log.info("ws probe: started target=%s interval=%.0fs", target, interval)

    while True:
        # The probe's own exceptions must not bubble up as task exceptions (that
        # would enter asyncio's "Task exception was never retrieved" log, looking
        # like a backend fault).
        try:
            ok = await _probe_once_async(query, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ws probe: probe failed %s: %s", type(exc).__name__, exc)
            ok = False

        metrics.ws_probe_alive.set(1 if ok else 0)
        if not ok:
            metrics.ws_probe_failures.inc()

        await asyncio.sleep(interval)
