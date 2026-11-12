"""WS outbound fan-out hub (ISSUE #71) -- one subscription, beat-driven, latest
frame only per robot.

[Why this is needed (root cause)]
Before the change, `main.py`'s `ws_robots` was "one implementation per
connection":
  1. each WebSocket independently did `r.pubsub()` on `robot:stream:all` +
     `robot:alerts`;
  2. for **every** telemetry received, it called `await ws.send_json(...)` once.
Scale = 100 units x 10Hz = 1000 msg/s. With N big-screens connected, the server
did `N x 1000` JSON serializations + sends per second, and Redis delivered the
same message once per subscriber -- CPU/memory grew **linearly** with the client
count, coupled to a signal that "how many screens are open" should otherwise be
stable.

[This module's approach (merged design with the ISSUE #73 backpressure layer)]
Split "subscribe", "coalesce" and "deliver" so each happens only once:
  1. **Shared subscription**: the whole process has **one** pubsub subscription
     to `chan_stream_all` and `chan_alerts` (replaces per-connection ones);
  2. **Node coalescing**: telemetry is overwritten into a "latest frame" dict
     `_latest` keyed by `robot_id` -- each robot has only one frame at any moment,
     intermediate frames are naturally deduped (coalesced_total counter);
  3. **Fixed-beat fan-out**: an independent `_broadcast_loop` every
     `ws_broadcast_interval_ms` (default 100ms = 10Hz) serializes the **last**
     frame of **each** robot in `_latest` **once**, then pushes the same string
     via `outbound.enqueue_raw()` into **each connection's own** bounded
     WsOutbound queue (#73 backpressure layer) -- each connection's sender
     coroutine serializes `send_text`.

Why the beat only `put_nowait` and does not directly `await ws.send_text`:
If the hub sent directly, one slow client would block **the broadcast beat
itself** and starve all healthy connections -- exactly the pathology #73 killed
at the single-connection scale, recurring at the process scale. put_nowait never
blocks: a slow client only fills its own queue and drops only its own old
telemetry, while alerts still go through the headroom (undroppable); beat
duration is decoupled from any client's speed.

So the server's subscription count drops from N to **1**, and serialization
drops from N x 1000/s to **1000/s once per process** (the string is reused
across connections); sending is still backstopped by #73's bounded queues.
Each robot still appears at 10Hz externally, and the envelope contract is
unchanged field-by-field.

[Why telemetry coalesces by id but alerts do not (contract surface)]
spec/00 SS5.2's telemetry envelope is a **single** `{"type":"telemetry","data":{one unit}}`,
and the frontend `useRobotWebSocket.ts`'s `latestDataRef.set(robot_id, item)` is
itself "overwrite same id" semantics -- the server doing the same thing early
keeps the external contract **unchanged field-by-field**.
Alerts are an **event stream** (the frontend `pushAlert` prepends each one, and
order matters), so they cannot be merged into an array (that would change the
envelope) nor dropped; they go through the same beat and are forwarded verbatim
-- the `chan_alerts` forward is the **single source** of the frontend alert
stream and **must not be removed** (ISSUE #71 forbidden item).

[Forbidden-item self-check (aligns with ISSUE #71 "constraints / forbidden")]
- Do not remove `chan_alerts` forwarding: see the alert branch in `_consume_loop`
  (forwarded within the same shared subscription);
- Do not break auth order: auth and the 1008 rejection stay in `ws_robots`; this
  module only emits frames **after** registration;
- Do not introduce a per-message extra DB query: this module only touches Redis
  pubsub and in-memory dicts -- **zero DB calls**.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app import metrics
from app.config import settings
from app.db.redis import get_redis
from app.ws_backpressure import WsOutbound

log = logging.getLogger("ws_hub")


def _envelope(kind: str, data: Any) -> str:
    """Build the unified envelope from spec/00 SS5.2.

    `separators` strips whitespace: at 1000 frames/s every comma/colon space is
    real byte overhead.
    `ensure_ascii=False` emits alert Chinese messages straight as UTF-8 (Redis
    pubsub already carries UTF-8 bytes; this layer should not rewrite anything).
    """
    return json.dumps({"type": kind, "data": data}, separators=(",", ":"), ensure_ascii=False)


class WsHub:
    """Process-level singleton: shared subscription + node coalescing + fixed-beat
    fan-out (through each connection's backpressure queue)."""

    def __init__(self) -> None:
        # What is registered is **each connection's outbound channel** (WsOutbound),
        # not a bare WebSocket: the hub only does enqueue_raw (put_nowait) and
        # delegates "a slow client only wrecks itself" to #73.
        self._clients: list[WsOutbound] = []
        # robot_id -> **latest** telemetry frame (value is the dict already of
        # decoded types, see state_cache._decode).
        self._latest: dict[str, dict] = {}
        # Alert event buffer: not coalesced, not dropped; forwarded verbatim on
        # the next beat.
        self._alerts: list[dict] = []
        # R-VLA-EVAL Phase 1: latest policy-action frame per robot (overwrite
        # semantics, same as telemetry). Coalesced into one frame per robot per
        # beat and broadcast as {"type":"action","data":{...}}.
        self._latest_action: dict[str, dict] = {}
        # [Deliberately no asyncio.Lock] All critical sections (snapshot+clear /
        # overwrite / registry add-remove) have **no await** inside, so on the
        # single-threaded event loop they are atomic by nature; uvicorn multi-
        # worker is multi-process (each holds its own singleton copy), so there is
        # no shared-memory contention. Adding a lock would instead introduce an
        # asyncio-primitive cross-event-loop binding problem: this singleton lives
        # across multiple TestClient sessions of the test suite (each session a new
        # loop), and once a Lock's first acquire binds to the old loop, the new
        # session hits RuntimeError on touch -- the same poison as
        # BatchWriter._drained.
        # Likewise start() rebuilds both tasks each time (see below), stop() clears
        # the containers.
        self._task: asyncio.Task | None = None
        self._redis_task: asyncio.Task | None = None

    # ------------------------------------------------------------ read-only snapshot (metrics)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def pending_robots(self) -> int:
        """Current number of robots pending fan-out (= accumulated latest frames
        since the last beat)."""
        return len(self._latest)

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the shared-subscription and beat-broadcast tasks. Repeated calls
        are idempotent."""
        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._broadcast_loop(), name="ws-hub-broadcast")
        self._redis_task = loop.create_task(self._consume_loop(), name="ws-hub-consume")
        log.info(
            "ws hub started: interval=%.0fms channel=%s alerts=%s",
            settings.ws_broadcast_interval_ms,
            settings.chan_stream_all,
            settings.chan_alerts,
        )

    async def stop(self) -> None:
        """Cancel the two background tasks and clear the buffers (shutdown)."""
        for task in (self._task, self._redis_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - teardown must not raise
                pass
        self._task = None
        self._redis_task = None
        self._clients.clear()
        self._latest.clear()
        self._alerts.clear()
        self._latest_action.clear()

    # ------------------------------------------------------------ connection registration

    async def register(self, outbound: WsOutbound) -> None:
        self._clients.append(outbound)

    async def unregister(self, outbound: WsOutbound) -> None:
        """Reclaim on connection teardown (ws_robots's finally). Repeated
        unregister does not raise, silently ok."""
        try:
            self._clients.remove(outbound)
        except ValueError:
            pass

    # ------------------------------------------------------------ fan-out

    async def broadcast_now(self, interval_s: float) -> None:
        """Push the current coalesced result into every connection's outbound
        queue (one beat's concrete action).

        **Each connection does not coalesce separately, nor serialize a second
        time here**: `_latest` and `_alerts` are snapshotted and cleared
        (synchronous section, atomic by nature, see __init__ comment), then
        serialized into frame strings **once**, and all connections reuse the same
        batch of strings via `enqueue_raw` -- one publish -> N connections reuse.
        The whole fan-out only does `put_nowait`, **with no await on any client**:
        a slow client at most fills its own queue; the beat itself is unaffected
        by any client's speed (#73 merge).
        """
        latest = list(self._latest.values())
        alerts = list(self._alerts)
        actions = list(self._latest_action.values())
        self._latest.clear()
        self._alerts.clear()
        self._latest_action.clear()
        clients = list(self._clients)

        if not clients or (not latest and not alerts and not actions):
            return

        # Single serialization: telemetry/action once per robot, alert once per
        # event, then N connections reuse the same string object.
        # `kind` here is the backpressure bucket: "telemetry" (droppable,
        # coalesced) or "alert" (undroppable, deferred). Actions reuse the
        # droppable bucket -- they are high-frequency control frames, not
        # must-not-lose alerts.
        frames: list[tuple[str, str]] = [
            ("telemetry", _envelope("telemetry", item)) for item in latest
        ]
        frames.extend(("alert", _envelope("alert", alert)) for alert in alerts)
        frames.extend(("telemetry", _envelope("action", action)) for action in actions)
        n_telem = len(latest)
        n_alert = len(alerts)
        n_action = len(actions)

        for ob in clients:
            for kind, text in frames:
                ob.enqueue_raw(kind, text)

        # Metrics: actual send volume and "the amount coalesced away".
        # frames are **one copy per connection**, so multiply by the connection
        # count -- this is exactly the volume to watch: before the change it was
        # ~ clients x 1000/s; after, ~ clients x (beats x robots), and the
        # absolute coalesced value directly proves "only the latest frame per
        # robot" is in effect.
        sent = len(clients)
        metrics.ws_hub_frames_total.labels(kind="telemetry").inc((n_telem + n_action) * sent)
        metrics.ws_hub_frames_total.labels(kind="alert").inc(n_alert * sent)

        log.debug(
            "ws hub tick: robots=%d alerts=%d clients=%d interval=%.3fs",
            n_telem,
            n_alert,
            sent,
            interval_s,
        )

    async def _broadcast_loop(self) -> None:
        interval = settings.ws_broadcast_interval_ms / 1000.0
        if interval <= 0:
            interval = 0.1
        # Fixed beat: interval depends only on config, **not on the message
        # count** -- this is exactly the "push on a fixed beat" the acceptance
        # requires, and the premise that CPU does not grow linearly with the
        # number of big-screens under load.
        while True:
            await asyncio.sleep(interval)
            try:
                await self.broadcast_now(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One beat exception must not let the whole broadcast loop exit
                # (otherwise all big-screens stop receiving frames from then on).
                log.exception("ws hub broadcast tick failed")

    # ------------------------------------------------------------ shared subscription (one copy)

    async def _consume_loop(self) -> None:
        """The **only** Redis pubsub consume loop for the whole process.

        The key difference from before: the subscription is built **once**, not
        once per connection. On disconnect (Redis jitter) it backs off and
        reconnects -- without it, a single Redis reconnect would leave all
        big-screens silent forever (while the connections themselves stay open,
        showing "fake green").
        """
        delay = 1.0
        while True:
            pubsub = None
            try:
                r = await get_redis()
                pubsub = r.pubsub()
                await pubsub.subscribe(
                    settings.chan_stream_all, settings.chan_alerts, settings.chan_action_all
                )
                delay = 1.0
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    channel = msg["channel"]
                    if channel == settings.chan_alerts:
                        self._alerts.append(data)
                    elif channel == settings.chan_stream_all:
                        rid = data.get("robot_id") if isinstance(data, dict) else None
                        if rid:
                            # Overwrite = keep only the latest frame per robot
                            # (localized convergence).
                            # Count: **if this overwrites an unsent same-id frame,
                            # that is one coalesced away**.
                            if rid in self._latest:
                                metrics.ws_hub_coalesced_total.inc()
                            self._latest[rid] = data
                    elif channel == settings.chan_action_all:
                        rid = data.get("robot_id") if isinstance(data, dict) else None
                        if rid:
                            # Same overwrite/coalesce semantics as telemetry: only
                            # the latest action per robot is fanned out per beat.
                            if rid in self._latest_action:
                                metrics.ws_hub_coalesced_total.inc()
                            self._latest_action[rid] = data
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("ws hub redis loop error, reconnect in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(
                            settings.chan_stream_all,
                            settings.chan_alerts,
                            settings.chan_action_all,
                        )
                        await pubsub.aclose()
                    except Exception:
                        pass


# Process-level singleton. main.py's lifespan owns start()/stop();
# ws_robots imports and uses it directly (same pattern as `writer = BatchWriter()`).
hub = WsHub()
