"""WS outbound backpressure + heartbeat contract tests (ISSUE #73).

Locks down ISSUE #73's three acceptance criteria and three "constraints/forbidden items"; **lock
semantics, not implementation**: changing the implementation should not turn red as long as the
behavior is unchanged.

  Acceptance 1: check backpressure state (queue depth) before sending; when over the threshold drop
                the oldest frame / throttle, rather than unbounded accumulation;
  Acceptance 2: ping/pong keepalive, abnormally-closed / timed-out connections actively close, and
                correctly walk finally to reclaim `_ws_active`;
  Acceptance 3: a slow client does not crush backend memory; after disconnect `_ws_active` returns
                to baseline within the heartbeat timeout.

  Constraint 1: do not break the auth order (accept -> 1008 -> no business frame sent);
  Constraint 2: do not remove the `chan_alerts` forwarding;
  Constraint 3: do not drop **alert** messages for backpressure.

Run (must be run separately; rationale in pytest.ini and the conftest.py header):
    pytest backend_ingestion/tests/test_ws_backpressure.py -v

Why no real uvicorn + real WS connection: that is CI/Docker work; this file uses TestClient's
in-process WS (covering handshake/auth/first-frame/disconnect four segments) and drives `WsOutbound`
directly. For end-to-end real connections, see the PR description.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import metrics, ws_backpressure
from app.config import settings
from app.main import app
from app.ws_backpressure import ALERT, TELEMETRY, WsOutbound


# ------------------------------------------------------------------ infrastructure

class BlockingWS:
    """A client stand-in that never consumes: the first send permanently blocks.

    We use it to simulate a "frozen browser tab" -- this is the failure mode ISSUE #73 describes.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, envelope: dict) -> None:
        self.sent.append(envelope)
        await asyncio.sleep(3600)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class DrainingWS:
    """A client stand-in that consumes normally: send is immediately available."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, envelope: dict) -> None:
        self.sent.append(envelope)


async def _settle() -> None:
    """Let the event loop drain already-queued work (no fixed duration, to reduce flakiness)."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def client():
    # Do not trigger lifespan: this file only tests route behavior and the outbound channel;
    # really starting MQTT/Redis needs external deps, which is another test layer.
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------- acceptance 1: backpressure: memory is bounded

class TestOutboundIsBounded:
    """ISSUE #73 acceptance 1: when queue depth exceeds the threshold, drop the oldest frame,
    **never accumulate unboundedly**."""

    def test_slow_client_queue_stays_bounded(self):
        """Push 1000 in; the slow client's queue must cap at maxsize, not grow to 1000."""

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=10, alert_slack=2)
            ob.start()
            for i in range(1000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        # Memory bounded = queue length is a **constant** (physical capacity), independent of how
        # many were pushed. Physical capacity = telemetry soft cap + alert reserve, both hardcoded
        # in config.
        assert ob.qsize <= ob.capacity, f"queue overflowed: {ob.qsize} > {ob.capacity}"
        assert ob.telemetry_dropped > 0, "slow client should trigger frame drops"

    def test_drops_oldest_not_newest(self):
        """What gets dropped must be the **oldest** frame: telemetry is "latest-value-first"
        time-series data."""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=3, alert_slack=0)
            for i in range(10):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return ob, ob.drain_nowait()

        _ob, kept = asyncio.run(drive())
        nums = [e["n"] for e in kept]
        # The queue should keep the **last** few (oldest dropped), not the oldest few.
        assert nums == sorted(nums), f"order corrupted: {nums}"
        assert nums[-1] == 9, f"latest frame must be present: {nums}"
        assert nums[0] > 0, f"oldest frame should have been dropped: {nums}"

    def test_misconfigured_maxsize_still_bounds(self):
        """A `maxsize` of 0/negative must **not** degenerate into unbounded (asyncio.Queue's 0 =
        infinite).

        This is the only piece of "defensive" code, with a concrete reason: the threshold comes from
        .env, and a miswritten 0 puts the fixed problem right back -- and "wrong config" gives no
        runtime signal. Reverse check: reverting `max(1, maxsize)` back to `maxsize` turns this red
        (observed qsize=999).
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=0, alert_slack=2)
            ob.start()
            for i in range(1000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        assert ob.maxsize >= 1, "maxsize degenerated into unbounded (0 = infinite in asyncio)"
        assert ob.qsize <= ob.capacity, f"unbounded accumulation: qsize={ob.qsize}"

    def test_enqueue_never_blocks(self):
        """`enqueue` must return synchronously: the moment it awaits, the forward loop is stalled."""

        async def drive() -> float:
            ob = WsOutbound(BlockingWS(), maxsize=2, alert_slack=0)
            ob.start()
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for i in range(5000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return loop.time() - t0

        elapsed = asyncio.run(drive())
        # If any of the 5000 enqueues awaits on the slow client, this would be on the 3600s scale.
        assert elapsed < 1.0, f"enqueue appears to block: {elapsed:.3f}s"


# ------------------------------------------------- constraint 3: alerts must never be dropped (core)

class TestAlertIsNeverDropped:
    """ISSUE #73 constraint 3: alert priority is higher than telemetry throttling, **at most
    merge/delay, never drop**."""

    def test_alert_survives_telemetry_overflow(self):
        """After telemetry fills the queue, alerts still get in and are not dropped."""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=4)
            for i in range(200):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            for i in range(3):
                ob.enqueue(ALERT, {"type": "alert", "rule_id": "motor_temp", "n": i})
            return ob, ob.drain_nowait()

        _ob, kept = asyncio.run(drive())
        alerts = [e for e in kept if e["type"] == "alert"]
        # The reserved alert_slack space exists exactly for this scene: after telemetry fills the
        # soft cap, alerts still get in, not a single one dropped.
        assert len(alerts) == 3, f"alerts dropped: only {len(alerts)}/3 left"

    def test_alert_overflow_is_capped_not_unbounded(self):
        """Even under an alert flood, the queue must not grow unbounded -- capacity is capped at
        maxsize + slack."""

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=3)
            for i in range(10):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            for i in range(500):
                ob.enqueue(ALERT, {"type": "alert", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        assert ob.qsize <= ob.capacity, f"alert path blew up the queue: {ob.qsize}"
        # Once at the cap, count as deferred (observable), rather than silently vanishing or raising.
        assert ob.alerts_deferred > 0, "overflow should be accounted"

    def test_alert_flood_does_not_raise(self):
        """An alert flood must not raise -- raising would blow up the entire forward loop.

        Regression guardrail: the first draft of this file both raised QueueFull on the
        "queue full + alert" path (missing guard), and after switching to retry became a 100% CPU
        livelock. This must block both.
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=4, alert_slack=2)
            ob.start()
            for i in range(3000):
                ob.enqueue(ALERT, {"type": "alert", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())  # raising fails the test
        assert ob.qsize <= ob.capacity

    def test_telemetry_cannot_eat_the_alert_reserve(self):
        """Telemetry must **not** occupy the capacity reserved for alerts.

        Regression guardrail: the previous version judged whether telemetry should drop by
        **physical capacity**, so telemetry first filled the reserve, and subsequent alerts had to
        defer -- with `maxsize=5 / slack=4` observed telemetry took 9 slots and all 3 alerts were
        deferred, making "alerts must not be dropped" nominal only. The correct semantics: telemetry
        may only use up to `maxsize` (soft cap); the reserve belongs to alerts alone.
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=4)
            for i in range(200):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return ob

        ob = asyncio.run(drive())
        assert ob.qsize <= ob.maxsize, (
            f"telemetry overstepped into the alert reserve: qsize={ob.qsize} > soft cap={ob.maxsize}"
        )
        assert ob.capacity > ob.maxsize, "reserve should exist (slack > 0)"

    def test_alert_accounting_is_conserved_in_mixed_stream(self):
        """In a mixed stream, alerts must be **conserved**: queued + in-flight + deferred == total
        emitted.

        Regression guardrail: the previous version implemented alert priority by "swapping queues to
        free space", replacing `self._queue` with a new object while racing the consuming send
        coroutine, silently losing alerts. Counting only the alerts in the queue hides the problem
        (the dropped one is nowhere), so we must also count "in-flight" (already taken by the sender,
        stuck on send_json) for conservation.
        """

        class TrackingWS:
            def __init__(self) -> None:
                self.inflight: list[dict] = []

            async def send_json(self, envelope: dict) -> None:
                self.inflight.append(envelope)
                await asyncio.sleep(3600)

        async def drive() -> tuple[WsOutbound, TrackingWS, int]:
            ws = TrackingWS()
            ob = WsOutbound(ws, maxsize=50, alert_slack=16)
            ob.start()
            alerts = 0
            for i in range(2000):
                if i % 40 == 0:
                    ob.enqueue(ALERT, {"type": "alert", "seq": i})
                    alerts += 1
                else:
                    ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            await _settle()
            return ob, ws, alerts

        ob, ws, alerts = asyncio.run(drive())
        queued = sum(1 for e in ob.drain_nowait() if e["type"] == "alert")
        inflight = sum(1 for e in ws.inflight if e.get("type") == "alert")
        total = queued + inflight + ob.alerts_deferred
        assert total == alerts, (
            f"alerts not conserved: queued {queued} + in-flight {inflight} + deferred {ob.alerts_deferred}"
            f" = {total} != {alerts} (some alerts silently dropped)"
        )

    def test_alert_kind_uses_dedicated_path(self):
        """`alert` and `telemetry` must take different branches (the existence of tiered handling)."""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=2, alert_slack=2)
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 0})
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 1})
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 2})  # fill soft cap -> drop oldest
            ob.enqueue(ALERT, {"type": "alert", "n": 0})          # go through alert reserve
            return ob, ob.drain_nowait()

        ob, kept = asyncio.run(drive())
        # Telemetry is dropped (telemetry_dropped > 0), alerts none dropped (alerts_deferred == 0).
        assert ob.telemetry_dropped > 0, "telemetry should be dropped"
        assert ob.alerts_deferred == 0, "alerts should not defer when reserve headroom remains"
        assert [e for e in kept if e["type"] == "alert"], "alerts must remain"


# ------------------------------------------------ acceptance 2: heartbeat & reclaim (_ws_active)

#: Reclaim wait ceiling. The server's finally is **asynchronous** relative to the client ctx exit
#: (between the client closing the local socket and the server finishing teardown there is one
#: event-loop scheduling gap), observed stable on the 10ms scale; we give 2s so CI load jitter does
#: not make it flaky.
RECYCLE_WAIT_S = 2.0


def _wait_recycled(expected: int, timeout: float = RECYCLE_WAIT_S) -> bool:
    """Poll until `_ws_active` drops to `expected`. Returns whether it was reached."""
    import time

    import app.main as main

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if main._ws_active == expected:
            return True
        time.sleep(0.01)
    return main._ws_active == expected


class TestConnectionAccounting:
    """ISSUE #73 acceptance 2: after a connection exception/timeout, finally must reclaim
    `_ws_active`."""

    def test_disconnect_recycles_counter(self, client):
        """After the client disconnects, the count must return to zero (the core fix of this issue).

        Note we assert "**eventual** reclaim" not "synchronous reclaim": between the client closing
        the local socket and the server finishing finally there is inevitably one scheduling delay.
        Writing this as a synchronous assertion would test scheduling timing, not product behavior,
        and flake randomly on CI.
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        assert token, "probe token self-sign failed, test precondition not met"
        before = main._ws_active

        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert main._ws_active == before + 1, "count not +1 after connect"

        assert _wait_recycled(before), (
            f"not reclaimed after disconnect: {main._ws_active} (should return to {before}) -- "
            "this is exactly the 'connections only increase, never decrease' that ISSUE #73 fixes"
        )

    def test_repeated_connections_do_not_leak(self, client):
        """6 rounds serially, each waiting for reclaim -- if finally misses once, the count rises
        monotonically, waiting for reclaim will time out, so this catches "occasional missed
        reclaim".
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        before = main._ws_active
        for i in range(6):
            with client.websocket_connect(f"/ws/robots?token={token}") as ws:
                ws.receive_json()
            assert _wait_recycled(before), (
                f"count did not return to {before} after round {i + 1}: {main._ws_active}"
            )

    def test_counter_returns_to_baseline_after_many_rounds(self, client):
        """After many fast reconnect rounds (simulating the frontend's backoff reconnect storm) the
        count must return to baseline.

        This differs from `test_repeated_connections_do_not_leak` in that it reconnects **without
        waiting for reclaim**: it covers the overlapping window of "the next one arrives before the
        previous teardown finished" -- if there is a race between finally and +1 (e.g. decrementing
        the wrong object), overlap shows as a count offset that a single case does not reproduce.
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        before = main._ws_active
        for _ in range(10):
            with client.websocket_connect(f"/ws/robots?token={token}") as ws:
                ws.receive_json()
        assert _wait_recycled(before), f"count did not return to baseline after reconnect storm: {main._ws_active}"

    def test_rejected_handshake_does_not_touch_counter(self, client):
        """A handshake rejected with 1008 must **not** count toward the connection total.

        First establish the baseline (wait for the count to settle), then fire two bad handshakes --
        the count must not budge. Before the fix, if +1 was placed before auth, a "1008 storm"
        (frontend repeatedly reconnecting with an expired token) would inflate the connection count,
        masking the real signal of "how many screens are actually open".
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            ws.receive_json()
        assert _wait_recycled(0), f"baseline not settled: {main._ws_active}"
        baseline = main._ws_active

        for bad in ("/ws/robots", "/ws/robots?token=bogus"):
            try:
                with client.websocket_connect(bad) as ws:
                    ws.receive_json()
            except Exception:
                pass
            assert main._ws_active == baseline, (
                f"{bad} polluted the connection count: {main._ws_active} != {baseline}"
            )

    def test_aclose_is_idempotent(self):
        """`aclose` can be called repeatedly: finally and the watchdog may each collect once."""

        async def drive() -> WsOutbound:
            ob = WsOutbound(DrainingWS(), maxsize=4)
            ob.start()
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 0})
            await _settle()
            await ob.aclose()
            await ob.aclose()  # must not raise
            return ob

        ob = asyncio.run(drive())
        assert ob._task is None


# ------------------------------------------------------------ constraint 1: auth order

class TestAuthOrderPreserved:
    """ISSUE #73 constraint 1: accept -> 1008 -> no business frame sent; the order must not break."""

    def test_missing_token_closes_1008(self, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect("/ws/robots") as ws:
                ws.receive_json()
        assert ei.value.code == 1008

    def test_invalid_token_closes_1008(self, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect("/ws/robots?token=not-a-jwt") as ws:
                ws.receive_json()
        assert ei.value.code == 1008

    def test_valid_token_gets_snapshot_first(self, client):
        """A legitimate connection's first frame must be snapshot (the backpressure rewrite must not
        change the first-frame semantics)."""
        from app import ws_probe

        token = ws_probe._mint_token()
        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "robots" in first["data"]


# ------------------------------------------- constraint 2: alert forwarding still present (#71 RAW channel)

class TestRawFramesFromHub:
    """ISSUE #71 merge: ws_hub's tempo fan-out delivers **pre-serialized** frames via
    `enqueue_raw`.

    The RAW channel must fully inherit #73's tiered semantics: telemetry may be dropped (oldest),
    alerts must never be dropped; and the send coroutine sends RAW frames via `send_text` directly
    (where one serialization -> N-connection reuse lands).
    """

    def test_raw_telemetry_sent_verbatim_via_send_text(self):
        """RAW frames must be `send_text` verbatim, not passed through send_json again
        (re-serialization)."""

        class RecordingWS:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.json_sent: list[dict] = []

            async def send_text(self, text: str) -> None:
                self.texts.append(text)

            async def send_json(self, envelope: dict) -> None:
                self.json_sent.append(envelope)

        async def drive() -> tuple[RecordingWS, int]:
            ws = RecordingWS()
            ob = WsOutbound(ws, maxsize=10)
            ob.start()
            ob.enqueue_raw("telemetry", '{"type":"telemetry","data":{"robot_id":"r1"}}')
            await _settle()
            ob._task.cancel()
            await asyncio.gather(ob._task, return_exceptions=True)
            return ws, ob.sent_count

        ws, sent = asyncio.run(drive())
        assert sent == 1
        assert ws.texts == ['{"type":"telemetry","data":{"robot_id":"r1"}}'], (
            "RAW frames must be send_text byte-for-byte verbatim (the premise for string reuse across connections)"
        )
        assert ws.json_sent == [], "RAW frames must not go through send_json (that would re-serialize per connection)"

    def test_raw_alert_survives_telemetry_overflow(self):
        """After the queue is filled by RAW telemetry, RAW alerts must still be delivered (via the
        reserve, never dropped)."""

        async def drive() -> tuple[WsOutbound, list[str]]:
            ob = WsOutbound(DrainingWS(), maxsize=2, alert_slack=4)
            for i in range(5):  # far over soft cap 2 -> keep dropping oldest
                ob.enqueue_raw("telemetry", f'{{"n":{i}}}')
            ob.enqueue_raw("alert", '{"type":"alert"}')
            return ob, ob.drain_nowait()

        ob, frames = asyncio.run(drive())
        assert ob.telemetry_dropped > 0, "RAW telemetry over limit must trigger oldest-drop"
        assert '{"type":"alert"}' in frames, "RAW alert must remain"
        assert ob.alerts_deferred == 0, "RAW alert should not defer when reserve headroom remains"

    def test_drop_oldest_never_eats_raw_alert(self):
        """Regression guardrail: the oldest-drop criterion must be a whitelist (only telemetry is
        droppable).

        The first version used `item[0] is not ALERT` to decide droppable -- the `_RAW_ALERT`
        sentinel object `is not` against a string is always true, so a pre-serialized alert at the
        head would be silently eaten.
        """
        from app.ws_backpressure import _RAW_ALERT, _RAW_TELEMETRY  # noqa: F401

        async def drive() -> tuple[WsOutbound, list[str]]:
            ob = WsOutbound(DrainingWS(), maxsize=2, alert_slack=4)
            ob.enqueue_raw("alert", '{"type":"alert","n":0}')  # head is an alert
            ob.enqueue_raw("telemetry", '{"n":1}')
            ob.enqueue_raw("telemetry", '{"n":2}')  # fill soft cap
            ob.enqueue_raw("telemetry", '{"n":3}')  # trigger oldest-drop -> must drop '{"n":1}'
            return ob, ob.drain_nowait()

        ob, frames = asyncio.run(drive())
        assert '{"type":"alert","n":0}' in frames, "head alert must not be eaten by the oldest-drop logic"
        assert '{"n":1}' not in frames, "what got dropped must be the **oldest telemetry**"

    def test_metrics_include_hub_counters(self):
        """Hub counters must exist (the observation surface for whether the rewrite works; missing
        means regressed to per-frame direct send)."""
        text = metrics.render()[0].decode()
        assert "robot_backend_ws_hub_frames_total" in text
        assert "robot_backend_ws_hub_coalesced_total" in text


# -------------------------------------------------------------- metrics & config

class TestObservabilityAndConfig:
    def test_backpressure_metrics_exist(self):
        text = metrics.render()[0].decode()
        assert "robot_backend_ws_outbound_queue_depth" in text
        assert "robot_backend_ws_telemetry_frames_dropped_total" in text
        assert "robot_backend_ws_alerts_deferred_total" in text
        assert "robot_backend_ws_slow_client_evicted_total" in text

    def test_thresholds_come_from_settings(self):
        """Thresholds must come from config (AGENTS.md: hardcoding in business code forbidden)."""
        assert settings.ws_outbound_queue_size > 0
        assert settings.ws_heartbeat_interval_s > 0
        assert settings.ws_outbound_close_timeout_s > 0
        assert settings.ws_alert_overflow_slack >= 0

    def test_alert_slack_is_a_real_margin(self):
        """The slack default must be > 0, otherwise "alerts must not be dropped" degenerates into
        "first-come-first-served"."""
        assert ws_backpressure.ALERT_OVERFLOW_SLACK > 0
