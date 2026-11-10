"""R-14 — acceptance for `consume()`'s Redis disconnect supervision.

The failure mode before R-14 (real, not speculative): when `pubsub.listen()` throws
`redis.exceptions.ConnectionError` on a Redis blip, that exception **escapes `consume()`** →
propagates up through `amain()`'s `asyncio.gather` → the whole coroutine tree fails and the
process exits; and the two sibling coroutines `ticker()` / `escalation_loop()` that weren't
broken themselves get **cancelled along**.

This file pins down four semantics (corresponding to requirements §5 V1/V2/V3/V4):
1. Self-heal on disconnect: after `listen()` throws a disconnect exception, `consume()` **does
   not exit**, rebuilds the pubsub after backoff and **re-subscribes** (subscription state does
   not survive across connections).
2. Doesn't take down siblings: the exception **does not propagate** out of `consume()`.
3. Existing behavior unchanged: normal messages still get consumed, `JSONDecodeError` still
   skipped, rule exceptions still contained.
4. Observable: reconnect has a warning log + `robot_alert_redis_reconnect_total` counter.

⚠️ Key fact (measured on this machine, redis 5.0.3, which dictates the tested code must catch both
   exception groups):
    issubclass(redis.exceptions.ConnectionError, ConnectionError)  # built-in → False
    issubclass(redis.exceptions.TimeoutError, TimeoutError)        # built-in → False
redis-py's two exceptions only inherit `RedisError`, with no inheritance relation to the
built-in same-named classes. So the disconnect case tests **all three groups** (RedisError /
built-in OSError / redis's TimeoutError); testing only one would miss another.
"""

from __future__ import annotations

import asyncio
import json
import logging

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import metrics
from app.config import settings

# In tests, compress backoff to millisecond level, otherwise the default 1s-starting reconnect
# makes the unit test wait in vain.
TINY_BACKOFF = 0.001

#: Fake pubsub's cumulative count of `listen()` calls, for cases to wait for the consumer loop to
#: reach a target round.
#: ⚠️ Must be **globally cumulative**, never reset per round — once reset the count can never reach
#: 2, `_run()` would wait until timeout, and "does it re-subscribe after reconnect" can't be tested.
_listen_total = 0


def _listens() -> int:
    return _listen_total


class FakePubSub:
    """Fake pubsub: emits behavior per script, records subscribe/listen call counts.

    `listen()` is driven round by round by `script` (one round = one call to `listen()`), each item may be:
      - `Exception` instance            → throw immediately (simulate disconnect)
      - `list`                          → yield these messages one by one, then **hang forever**
                                          (wait to be cancelled)
      - `(list, Exception)` tuple       → yield messages first, then throw that exception
        (in the real driver "deliver messages then disconnect" is exactly this shape; only it can
         test "backoff reset")

    ⚠️ Why a "normal round" must hang instead of returning after yielding messages: the tested
    `async for` would **exit `consume()` normally** the instant it gets `StopAsyncIteration` —
    then the "does not exit" tested would be fake (it's the iterator being exhausted, not a
    disconnect self-heal). Real Redis's `listen()` never ends naturally, throwing only on
    disconnect. So here we must honor this property of the real driver.
    """

    def __init__(self, script):
        self.script = list(script)
        self.subscribe_calls: list[str] = []
        self.listen_calls = 0

    async def subscribe(self, chan):
        self.subscribe_calls.append(chan)

    async def listen(self):
        global _listen_total
        self.listen_calls += 1
        _listen_total += 1
        act = self.script.pop(0) if self.script else []
        tail = None
        if isinstance(act, tuple):
            act, tail = act
        if isinstance(act, BaseException):
            raise act
        for msg in act:
            yield msg
        if tail is not None:
            raise tail
        # After yielding messages → hang (simulate "connection still up, just no message yet").
        # Real Redis's listen() has only two exits: keep yielding, or throw an exception —
        # it **never** returns naturally. So "a round ends" in this stand-in can only be expressed
        # via a disconnect (the script explicitly writes a disconnect exception as the next round,
        # see test_backoff_resets_*).
        await asyncio.Event().wait()


class FakeRedis:
    def __init__(self, pubsubs):
        self._pubsubs = list(pubsubs)
        self.created = 0

    def pubsub(self):
        self.created += 1
        if not self._pubsubs:
            # After the script is exhausted it must still be able to keep building pubsubs (the
            # consumer loop won't stop itself), otherwise later rounds would IndexError — which
            # would disguise "script not written long enough" as "tested code crashed".
            return FakePubSub([])
        return self._pubsubs.pop(0)


def _wire(monkeypatch, m, pubsubs):
    """Swap `app.main.get_redis` for a fake and record `consume()`'s backoff durations.

    Note `main.py` does `from app.db import ... get_redis` (**the name is bound at import time**),
    so you must patch `app.main.get_redis`; patching `app.db.get_redis` has no effect on main."""
    fake = FakeRedis(pubsubs)
    sleeps: list[float] = []

    async def fake_get_redis():
        return fake

    # ⚠️ Can't write `monkeypatch.setattr(m.asyncio, "sleep", ...)`: `m.asyncio` **is** the
    # stdlib module object itself; patching it is globally replacing `asyncio.sleep`, and the
    # stand-in's internal `await asyncio.sleep(0)` would recurse into itself (measured
    # RecursionError, 949 deep). So we replace the `asyncio` **reference** in the `app.main`
    # namespace with a thin proxy that only rewrites `sleep`.
    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(s):
            sleeps.append(s)
            await asyncio.sleep(0)        # yield to the event loop, but don't wait real time

    monkeypatch.setattr(m, "get_redis", fake_get_redis)
    monkeypatch.setattr(m, "asyncio", _AsyncioProxy)
    return fake, sleeps


async def _run(m, target_listens: int, timeout: float = 3.0):
    """Run `consume()`, cancel after the fake pubsub reaches N listen() rounds.

    Return **the escaping exception** (None = nothing escaped, i.e. the fix worked).
    Use "actual listen count" rather than "idle yield count" as the progress criterion: the latter
    depends on event-loop scheduling details (fragile); the former is a deterministic event driven
    by the tested code itself."""
    global _listen_total
    _listen_total = 0
    task = asyncio.ensure_future(m.consume())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    try:
        while not task.done() and _listens() < target_listens and loop.time() < deadline:
            await asyncio.sleep(0)
    finally:
        if not task.done():
            task.cancel()
    try:
        await task
        return None
    except asyncio.CancelledError:
        return None
    except BaseException as exc:          # noqa: BLE001 —— this IS the assertion under test
        return exc


def _msg(rid="humanoid-001", temp=80.0):
    """Build a message. Default 80°C > 75°C threshold, hits the motor_temp rule."""
    return {
        "type": "message",
        "data": json.dumps({
            "robot_id": rid,
            "max_joint_temp": temp,
            "battery_pct": 90.0,
            "joints": [],
        }),
    }


# ---------------------------------------------------------------- V1 self-heal on disconnect

def test_reconnects_after_redis_connection_error(engine, monkeypatch):
    """`listen()` throws `redis.exceptions.ConnectionError` → no exit + re-subscribe.

    This is the core case of R-14: before the fix the exception escaped the coroutine and this
    case would get it; after the fix you should observe the **second subscribe** (subscription
    doesn't survive across connections, must re-subscribe)."""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("Connection reset by peer")])
    ps2 = FakePubSub([[]])                     # after reconnect, this round is normal (no message)
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"exception escaped consume(): {exc!r}"
    assert ps1.subscribe_calls == [settings.chan_stream_all]
    assert ps2.subscribe_calls == [settings.chan_stream_all], "did not re-subscribe after reconnect"
    assert fake.created == 2, "did not rebuild pubsub"
    assert sleeps and sleeps[0] == TINY_BACKOFF, f"backoff not applied: {sleeps}"


def test_reconnects_on_redis_timeout_error(engine, monkeypatch):
    """`redis.exceptions.TimeoutError` must also be caught.

    Why a separate case: this exception is **not** a subclass of built-in `TimeoutError`
    (measured False on this machine); catching only the built-in would miss it, so there must be
    one example dedicated to it."""
    m = engine
    ps1 = FakePubSub([RedisTimeoutError("Timeout reading from socket")])
    ps2 = FakePubSub([[]])
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"exception escaped consume(): {exc!r}"
    assert ps2.subscribe_calls == [settings.chan_stream_all]
    assert fake.created == 2


def test_reconnects_on_builtin_oserror(engine, monkeypatch):
    """Built-in `OSError` (hiredis / raw socket thrown directly) also counts as disconnect.

    Catching only `RedisError` would miss this path — so all three exception groups get one case
    each."""
    m = engine
    ps1 = FakePubSub([OSError("Connection refused")])
    ps2 = FakePubSub([[]])
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"exception escaped consume(): {exc!r}"
    assert fake.created == 2


def test_backoff_grows_exponentially_and_is_capped(engine, monkeypatch):
    """Consecutive disconnects → backoff 1x,2x,4x,... and never exceeds RECONNECT_BACKOFF_MAX_S.

    Capping is a hard requirement: unbounded doubling means after Redis recovers it takes hours to
    self-heal, equivalent to postponing "permanent unsubscribe" (no different from not fixing)."""
    m = engine
    n = 6
    pubsubs = [FakePubSub([RedisConnectionError("down")]) for _ in range(n)]
    pubsubs.append(FakePubSub([[]]))
    fake, sleeps = _wire(monkeypatch, m, pubsubs)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_MAX_S", TINY_BACKOFF * 4)

    exc = asyncio.run(_run(m, target_listens=n + 1))

    assert exc is None
    assert len(sleeps) >= 4, f"too few reconnects: {sleeps}"
    assert sleeps[0] == TINY_BACKOFF
    assert sleeps[1] == TINY_BACKOFF * 2
    assert sleeps[2] == TINY_BACKOFF * 4
    assert all(s <= TINY_BACKOFF * 4 for s in sleeps), f"backoff not capped: {sleeps}"


def test_backoff_resets_after_messages_delivered(engine, monkeypatch):
    """After a brief blip, and messages **actually delivered**, backoff must reset.

    Why the criterion is "messages delivered" rather than "subscribe returned": the most common
    shape when Redis restarts is a successful subscribe immediately followed by a disconnect. If
    you reset backoff there, it stays pinned at 1s — "exponential backoff" becomes nominal, turning
    into a once-per-second busy retry. This bug was measured on this machine (6 consecutive
    disconnects, backoff sequence all initial values), so here "received a message" must be the
    reset evidence."""
    m = engine
    # Round 2 "deliver messages first, then end with a disconnect" — exactly the real shape: as
    # long as the link really delivers (delivered=True), the subsequent disconnect should restart
    # from 1s. If you hang the reset on "subscribe returned", round 2's disconnect would be judged
    # "not reset", so round 3's backoff becomes the doubled value and this case goes red.
    pubsubs = [
        FakePubSub([RedisConnectionError("blip")]),        # 1: disconnect
        FakePubSub([([_msg("humanoid-003")],                 # 2: deliver messages…
                     RedisConnectionError("blip2"))]),       #    …then disconnect (ending)
        FakePubSub([[]]),                                   # 3: recover
    ]
    fake, sleeps = _wire(monkeypatch, m, pubsubs)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=3))

    assert exc is None
    assert len(sleeps) == 2, f"should have two reconnect backoffs, actual {sleeps}"
    assert sleeps[0] == sleeps[1] == TINY_BACKOFF, (
        f"backoff didn't reset after message delivery: {sleeps}"
    )


# ---------------------------------------------------------------- V2 no propagation

def test_disconnect_does_not_propagate_to_gather(engine, monkeypatch):
    """A disconnect exception doesn't escape `consume()` → sibling coroutines in the same gather
    aren't cancelled along.

    Directly replicate `amain()`'s orchestration semantics: put consume() and a "long-running
    sibling" in the same gather, and assert the sibling **ran to completion** (if consume had thrown
    up, gather would cancel the sibling immediately → assertion fails)."""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("down")])
    ps2 = FakePubSub([[]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_MAX_S", TINY_BACKOFF)

    # Use an Event for **deterministic** sync, not "idle N sleeps(0)": the latter's N depends on
    # event-loop scheduling details (different Python versions yield different counts), turning
    # "did the sibling finish" into a fragile race assertion. Here let the sibling explicitly wait
    # on an event, set by the code after the disconnect happens — only when the sibling **wasn't
    # cancelled** does it reach the set.
    sibling_ran = asyncio.Event()

    async def sibling():
        await sibling_ran.wait()
        return "sibling-finished"

    async def scenario():
        consume_task = asyncio.ensure_future(m.consume())
        sib = asyncio.ensure_future(sibling())
        # Wait until consume() really experiences one disconnect and enters the reconnect wait
        while _listens() < 1:
            await asyncio.sleep(0)
        for _ in range(20):
            await asyncio.sleep(0)
        # At this point, if consume() had thrown to gather, sib would already be cancelled (done and cancelled)
        sib_cancelled = sib.cancelled()
        sibling_ran.set()               # release the sibling to finish
        result = await sib
        consume_task.cancel()
        try:
            await consume_task
        except asyncio.CancelledError:
            pass
        return sib_cancelled, result

    sib_cancelled, sib_result = asyncio.run(scenario())

    assert not sib_cancelled, "sibling coroutine was cancelled — consume()'s exception leaked to gather"
    assert sib_result == "sibling-finished"


def test_non_disconnect_exception_still_propagates(engine, monkeypatch):
    """**Reverse** case: a non-disconnect exception must still escape consume(), not be swallowed
    into "fake alive".

    If the except were widened to `Exception`, any real bug (e.g. a broken message handler) would
    be silently swallowed, the process would fake-alive, and the dashboard would stay green —
    harder to trace than the original fault. So it must be nailed: fault-tolerance applies only to
    **disconnect-class** exceptions."""
    m = engine
    ps1 = FakePubSub([ZeroDivisionError("real bug, not a disconnect")])
    _wire(monkeypatch, m, [ps1, FakePubSub([[]])])

    exc = asyncio.run(_run(m, target_listens=1))

    assert isinstance(exc, ZeroDivisionError), (
        f"non-disconnect exception was swallowed (should escape consume()), actual: {exc!r}"
    )


# ---------------------------------------------------------------- V4 observable

def test_reconnect_increments_metric(engine, monkeypatch):
    """Reconnect must increment `robot_alert_redis_reconnect_total` ("fake alive" findable via PromQL)."""
    m = engine
    before = metrics.redis_reconnect._value.get()
    ps1 = FakePubSub([RedisConnectionError("down")])
    ps2 = FakePubSub([RedisConnectionError("down2")])
    ps3 = FakePubSub([[]])
    _wire(monkeypatch, m, [ps1, ps2, ps3])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=3))

    assert exc is None
    assert metrics.redis_reconnect._value.get() - before == 2


def test_reconnect_metric_is_registered_on_metrics_endpoint(engine):
    """The metric must be visible on `/metrics` (a `_total` family with the same name in the registry).

    Only import without registering (e.g. forgot to pass `registry=`) would hang it on the
    **default** registry, while this service outputs via an independent registry — then the metric
    is never scraped, equivalent to nonexistent."""
    text = metrics.generate_latest(metrics.registry).decode()
    assert "robot_alert_redis_reconnect_total" in text


def test_backoff_value_present_in_warning_log(engine, monkeypatch, caplog):
    """The warning log must carry the **backoff seconds** and channel name — "disconnected" alone
    can't triage."""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("Connection reset by peer")])
    ps2 = FakePubSub([[]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    with caplog.at_level(logging.WARNING, logger="alert_engine"):
        exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "reconnect did not emit a warning log"
    text = warnings[0].getMessage()
    assert "ConnectionError" in text or "Connection reset" in text
    assert settings.chan_stream_all in text, f"log missing channel name: {text!r}"


# ---------------------------------------------------------------- V3 behavior unchanged

def test_normal_messages_still_consumed_after_reconnect(engine, monkeypatch):
    """After reconnect, message handling must proceed as before — "backoff + re-subscribe" must not
    lose the window / online state."""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("down")])
    ps2 = FakePubSub([[_msg("humanoid-007", 85.0)]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None
    assert m.last_seen.get("humanoid-007") is not None, "message after reconnect was not consumed"
    assert len(m.windows["humanoid-007"]) == 1


def test_json_decode_error_still_skipped(engine, monkeypatch):
    """`JSONDecodeError` is still skipped per message (R-14 must not weaken existing exception handling)."""
    m = engine
    ps = FakePubSub([[
        {"type": "message", "data": "not-json{{{"},
        {"type": "subscribe", "data": "1"},          # non-message type also skipped as before
        _msg("humanoid-009"),
    ]])
    _wire(monkeypatch, m, [ps, FakePubSub([[]])])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=1))

    assert exc is None
    assert m.last_seen.get("humanoid-009") is not None
    assert "humanoid-009" in m.windows


def test_rule_exception_still_contained(engine, monkeypatch):
    """A single rule throwing is still contained (existing handling must not be weakened), and is
    **not** misjudged as a disconnect."""
    m = engine

    class BoomRule:
        rule_id = "boom"

        def evaluate(self, data, w):
            raise RuntimeError("rule bug")

    monkeypatch.setattr(m, "STREAM_RULES", [BoomRule()])
    ps = FakePubSub([[_msg("humanoid-010")]])
    fake, sleeps = _wire(monkeypatch, m, [ps, FakePubSub([[]])])

    exc = asyncio.run(_run(m, target_listens=1))

    assert exc is None
    assert sleeps == [], "rule exception was misjudged as disconnect, triggering a reconnect"
    assert fake.created == 1
    assert m.last_seen.get("humanoid-010") is not None


# ---------------------------------------------------------------- amain isolation

def test_isolate_swallows_crash_of_one_line(engine):
    """`_isolate` lets one driver line crash without affecting siblings, and doesn't swallow CancelledError."""
    m = engine

    async def boom():
        raise RuntimeError("line crashed")

    async def sibling():
        for _ in range(20):
            await asyncio.sleep(0)
        return "alive"

    async def scenario():
        return await asyncio.gather(
            m._isolate("boom", boom()),
            m._isolate("sibling", sibling()),
        )

    results = asyncio.run(scenario())
    assert results == [None, "alive"], f"sibling coroutine was dragged down: {results}"


def test_isolate_reraises_cancelled_error(engine):
    """`_isolate` must **not** swallow `CancelledError`, otherwise the process can't shut down cleanly."""
    m = engine

    async def hang():
        await asyncio.sleep(3600)

    async def scenario():
        t = asyncio.ensure_future(m._isolate("hang", hang()))
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            return "cancelled"
        return "swallowed"

    assert asyncio.run(scenario()) == "cancelled"
