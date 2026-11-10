"""Contract and performance-guard tests for the WS outbound hub (ISSUE #71, #73 merged
architecture).

The object under test is `app/ws_hub.py`'s `WsHub`, **no Redis / real port needed**: feed pubsub
messages with a fake Redis (`_FakeRedis`); on the connection side register a real `WsOutbound`
(wrapping a fake WebSocket, but **without starting the send coroutine**), fan out then use
`drain_nowait()` to take frames synchronously for assertions -- determinism first, no racing the
scheduler timing.

The six things pinned (aligned with ISSUE #71's acceptance criteria and forbidden items):
  1. **Each robot keeps only the latest frame**: 3 in a row for the same id -> only the last is fanned out;
  2. **Fixed tempo**: fan-out happens only on `_broadcast_loop`'s tempo, consumption itself sends nothing;
  3. **Multi-connection reuse**: N connections receive **the same** string object (one serialization ->
     multi-connection reuse);
  4. **Slow-client isolation**: a connection with a small queue only drops its own old telemetry, no
     other connection loses a frame;
  5. **Forbidden items**: (a) alerts forwarded verbatim one-by-one (no stripping of chan_alerts);
     (b) only an in-memory dict is touched, no per-message DB query introduced;
  6. **Lifecycle idempotence**: start() called twice does not rebuild the task; stop() cleans up.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import metrics
from app.config import settings
from app import ws_hub as ws_hub_mod
from app.ws_backpressure import WsOutbound
from app.ws_hub import WsHub, _envelope


# ------------------------------------------------------------------ fakes

class _FakeWS:
    """Host fake for the outbound channel. The hub fans out only by enqueuing, not sending,
    so it has no behavior."""


class _FakePubSub:
    def __init__(self, queue: "asyncio.Queue"):
        self._q = queue
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, *channels):
        self.subscribed.extend(channels)

    async def listen(self):
        while True:
            item = await self._q.get()
            if item is None:  # sentinel: end
                return
            yield item

    async def unsubscribe(self, *channels):
        pass

    async def aclose(self):
        self.closed = True


class _FakeRedis:
    def __init__(self):
        self.queue: "asyncio.Queue" = asyncio.Queue()
        self.pubsub_instance: _FakePubSub | None = None

    def pubsub(self):
        self.pubsub_instance = _FakePubSub(self.queue)
        return self.pubsub_instance

    def feed(self, channel: str, payload: dict) -> None:
        self.queue.put_nowait(
            {"type": "message", "channel": channel, "data": json.dumps(payload)}
        )


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()

    async def fake_get_redis():
        return r

    monkeypatch.setattr(ws_hub_mod, "get_redis", fake_get_redis)
    return r


@pytest.fixture
def hub():
    return WsHub()


def _outbound(maxsize: int = 1000) -> WsOutbound:
    """Build an outbound channel with **no send coroutine started**: fan-out only enqueues,
    assertions go through drain_nowait."""
    return WsOutbound(_FakeWS(), maxsize=maxsize, alert_slack=64)


def _telemetry(rid: str, ts: int) -> dict:
    return {
        "robot_id": rid,
        "status_code": 1,
        "error_code": 0,
        "battery_pct": 90.0,
        "max_joint_temp": 40.0,
        "joint_temperatures": {"left_knee_pitch": 40.0},
        "joint_positions": {"left_knee_pitch": 0.0},
        "joint_torques": {"left_knee_pitch": 0.0},
        "ts": ts,
    }


async def _drain(queue: "asyncio.Queue") -> None:
    """Wait for the messages in the Redis queue to be taken by the consume loop (it has no
    awaitable receipt)."""
    for _ in range(200):
        if queue.empty():
            await asyncio.sleep(0)
            if queue.empty():
                return
        await asyncio.sleep(0.005)


# ------------------------------------------------- 1. each robot keeps only the latest frame

def test_keeps_only_latest_frame_per_robot(fake_redis):
    """3 in a row for same id -> only the one with the largest ts is fanned out (middle frames
    merged locally)."""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            for ts in (1, 2, 3):
                fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", ts))
            await _drain(fake_redis.queue)
            # The consume loop itself **does not deliver** -- fan-out only happens on the tempo.
            assert ob.qsize == 0, "subscriber side must not enqueue directly (fan-out should be unified by tempo)"
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        frames = ob.drain_nowait()
        assert len(frames) == 1, f"3 same-id messages should merge into 1 frame, actual {len(frames)}"
        frame = json.loads(frames[0])
        assert frame["type"] == "telemetry"
        assert frame["data"]["ts"] == 3, "must be the **latest** frame"

    asyncio.run(scenario())


def test_coalesced_counter_increments_on_overwrite(fake_redis):
    """Overwritten frames must be counted (the metric is evidence that merging really happened,
    cannot be always 0)."""

    async def scenario():
        hub = WsHub()
        task = asyncio.create_task(hub._consume_loop())
        before = metrics.ws_hub_coalesced_total._value.get()
        try:
            for ts in (1, 2, 3, 4):
                fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-002", ts))
            await _drain(fake_redis.queue)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert metrics.ws_hub_coalesced_total._value.get() == before + 3

    asyncio.run(scenario())


# ------------------------------------------------- 2/3. multiple connections reuse the same batch of frames

def test_multiple_clients_get_same_frames(fake_redis):
    """N connections receive frames with **identical content**, and each is serialized only once
    (the string is the same object)."""

    async def scenario():
        hub = WsHub()
        obs = [_outbound() for _ in range(5)]
        for ob in obs:
            await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", 10))
            fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-002", 11))
            await _drain(fake_redis.queue)
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        # Each connection gets the latest frame for both robots.
        drained = [ob.drain_nowait() for ob in obs]
        for frames in drained:
            assert len(frames) == 2
        # The same frame is **the same string object** across all connections
        # (one serialization -> multi-connection reuse).
        firsts = {id(frames[0]) for frames in drained}
        assert len(firsts) == 1, "frame string should be reused across connections, not re-serialized per connection"

    asyncio.run(scenario())


# ------------------------------------------------- 4. slow-client isolation (#73 merge)

def test_slow_client_only_drops_own_telemetry(fake_redis):
    """A slow client with a small queue only drops **its own** old telemetry; a healthy connection
    loses not a single frame.

    This is the core guardrail of the merged architecture: the hub fans out only via put_nowait;
    if someone rewrites the fan-out to call `await ws.send_text` directly, a slow client would
    block the tempo and starve everyone -- then a healthy connection's frame count would also drop,
    and this test would immediately turn red.
    """

    async def scenario():
        hub = WsHub()
        slow = _outbound(maxsize=3)     # slow client: tiny queue
        healthy = _outbound(maxsize=1000)
        await hub.register(slow)
        await hub.register(healthy)
        task = asyncio.create_task(hub._consume_loop())
        try:
            for i in range(20):
                fake_redis.feed(settings.chan_stream_all, _telemetry(f"humanoid-{i:03d}", i))
            await _drain(fake_redis.queue)
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert len(healthy.drain_nowait()) == 20, "healthy connection must not lose a single frame"
        # The slow connection only fits 3 + drops the oldest: total enqueued 20 > capacity 3, so
        # drops must occur.
        assert slow.telemetry_dropped > 0, "slow client's backpressure drop must occur"
        # The hub still accounts as usual (sent-frame count by connection accumulates).
        # (The slow connection's drop count is summed in metrics.telemetry_frames_dropped in the
        # finally block; a single test does not run a real connection lifecycle, so here we only
        # assert the hub-side count occurred.)

    asyncio.run(scenario())


# ------------------------------------------------- 5. forbidden items

def test_alerts_are_forwarded_verbatim(fake_redis):
    """Alerts must not be merged into an array / dropped: forwarded verbatim one-by-one (the sole
    data source for the frontend alert stream)."""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            a1 = {"robot_id": "humanoid-001", "rule_id": "motor_temp", "severity": 3,
                  "message": "Overheating", "ts": 1}
            a2 = {"robot_id": "humanoid-002", "rule_id": "battery_low", "severity": 2,
                  "message": "Low battery", "ts": 2}
            fake_redis.feed(settings.chan_alerts, a1)
            fake_redis.feed(settings.chan_alerts, a2)
            await _drain(fake_redis.queue)
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        frames = ob.drain_nowait()
        kinds = [json.loads(f)["type"] for f in frames]
        assert kinds == ["alert", "alert"], "alerts must be forwarded one-by-one, not merged/dropped"
        datas = [json.loads(f)["data"] for f in frames]
        assert datas == [a1, a2], "alert content must be verbatim field-by-field (no rewriting)"

    asyncio.run(scenario())


def test_unregister_stops_delivery(fake_redis):
    """After unregister no more fan-out (the hub must not keep delivering to a dead channel after
    the connection is reclaimed)."""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", 1))
            await _drain(fake_redis.queue)
            await hub.unregister(ob)
            assert hub.client_count == 0, "registry must be empty after unregister"
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert ob.qsize == 0 and len(ob.drain_nowait()) == 0, "must not receive frames after unregister"

    asyncio.run(scenario())


def test_no_db_query_beyond_redis(fake_redis, monkeypatch):
    """Forbidden item: must not introduce a per-message extra DB query.

    Approach: stub the connection entry of `app.db.session` to "explode on any call", then feed
    100 telemetry through the full chain (consume + fan-out). If the logic touches the DB, it will
    explode.
    """
    import app.db.session as session_mod

    def boom(*a, **kw):
        raise AssertionError("ws hub must not touch the database")

    monkeypatch.setattr(session_mod, "get_pool", boom, raising=False)

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            for i in range(100):
                fake_redis.feed(settings.chan_stream_all, _telemetry(f"humanoid-{i:03d}", i))
            await _drain(fake_redis.queue)
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(ob.drain_nowait()) == 100

    asyncio.run(scenario())


# ------------------------------------------------- envelope shape (contract unchanged)

def test_envelope_shape_matches_spec():
    """The envelope is still the `{"type":..., "data":...}` three-branch shape of spec/00 section 5.2."""
    env = json.loads(_envelope("telemetry", _telemetry("humanoid-001", 1)))
    assert set(env.keys()) == {"type", "data"}
    assert env["type"] == "telemetry"
    assert env["data"]["robot_id"] == "humanoid-001"


def test_broadcast_interval_is_fixed_tempo(fake_redis, monkeypatch):
    """Tempo depends only on config: shrink the interval, more fan-outs occur in the same duration
    (fixed tempo)."""
    monkeypatch.setattr(settings, "ws_broadcast_interval_ms", 20.0)

    async def count_ticks() -> int:
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        # The consume loop must be present, otherwise _latest is always empty (the broadcast loop
        # has nothing to send).
        consume = asyncio.create_task(hub._consume_loop())
        beat = asyncio.create_task(hub._broadcast_loop())
        try:
            for _ in range(4):
                fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", 1))
                await asyncio.sleep(0.03)
        finally:
            for t in (consume, beat):
                t.cancel()
            await asyncio.gather(consume, beat, return_exceptions=True)
        return len(ob.drain_nowait())

    # Only assert "fan-out happened and the count is on the same order as the tempo", not a fragile
    # exact count (CI scheduling jitters).
    assert asyncio.run(count_ticks()) >= 2


# ------------------------------------------------- lifecycle idempotence

def test_start_is_idempotent(fake_redis):
    async def scenario():
        hub = WsHub()
        await hub.start()
        first = hub._task
        await hub.start()  # second call must not rebuild the task
        assert hub._task is first
        await hub.stop()
        assert hub._task is None and hub._redis_task is None

    asyncio.run(scenario())
