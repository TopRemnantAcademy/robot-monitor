"""Order 04 (Issue #4) acceptance tests: 4 rules / offline ticker / dedup idempotence / persist + publish.

This file fills a gap **previously untested**: the repo's existing cases cover the alert
closed-loop layer (inhibition order, silence order, persistence-failure counting), but **not a
single one** actually feeds telemetry into the 4 rules, nor proves the deduplicator is
idempotent or that store really writes robot_alert and publishes robot:alerts.

The object under test is the real implementation, not a fake:
- `should_emit` → real `app.deduplicator` with fakeredis standing in for Redis;
  `SET NX` is Redis-server-side semantics that **must** be verified by real Redis semantics
  (fakeredis) — an in-process dict fake can't test "idempotent".
- `save` → real `app.store` with "SQL-recording fake connection + PUBLISH-recording fake Redis",
  asserting SQL text, argument sequence, and channel name rather than "the function exists".
- `ticker` → real `app.main.ticker`, running **one real round** (not mocking away the judgement),
  then dialing `last_seen` back 11s to simulate "10s without telemetry".

Basis: `orders/04-alert-engine.md` §acceptance criteria, `spec/02` §3/§3.1/§4/§5.
"""

from __future__ import annotations

import time
from collections import deque

import fakeredis
import pytest

from tests.conftest import run

from app import deduplicator
from app import store as store_mod
from app.config import settings
from app.rules.base import Rule
from app.rules.battery import BatteryLowRule
from app.rules.joint_stuck import JointStuckRule
from app.rules.motor_temp import MotorTempRule
from app.rules.offline import OfflineRule


# ---------------------------------------------------------------- payload construction

def payload(rid: str = "humanoid-001", *, temp: float = 40.0,
            batt: float = 80.0, angle: float = 0.0,
            torque: float = 1.0) -> dict:
    """Build one telemetry record per spec/01 §3 field names (24-joint key names from the
    simulator's standard key table).

    Building just one joint's dict suffices: all 4 rules work on "judge whatever keys are present",
    `motor_temp` takes max, `joint_stuck` takes `state`'s key set — they don't depend on a fixed
    24 joints."""
    return {
        "robot_id": rid,
        "status_code": 1,
        "error_code": 0,
        "battery_pct": batt,
        "battery_temp": 35.0,
        "max_joint_temp": temp,
        "joint_temperatures": {"head_yaw": temp},
        "joint_positions": {"head_yaw": angle},
        "joint_torques": {"head_yaw": torque},
        "ts": int(time.time() * 1000),
    }


# ---------------------------------------------------------------- 1. all five rules present

def test_all_four_rules_exist_and_inherit_base():
    """Acceptance criterion 1: all rule classes present and inherit the Rule base (joint_stuck
    must not be omitted).

    Assert directly against `app.main.RULES` — that's the list the engine **actually schedules**;
    importing the rule classes without asserting the registry would stay green even if one were
    dropped.

    NOTE: the registry carries five rules (motor_temp / battery_low / joint_stuck / offline /
    anomaly); anomaly was added after the original "four-rule" wording — this assertion tracks the
    real registry rather than a stale count."""
    import app.main as m

    ids = sorted(r.rule_id for r in m.RULES)
    assert ids == ["anomaly", "battery_low", "joint_stuck", "motor_temp", "offline"]
    for r in m.RULES:
        assert isinstance(r, Rule), f"{r.rule_id} does not inherit the Rule base"
    # severity / dedup window checked one-by-one against the orders/04 table and config
    assert {r.rule_id: (r.severity, r.dedup_window_s) for r in m.RULES} == {
        "motor_temp": (3, 60),
        "battery_low": (2, 120),
        "offline": (3, 30),
        "joint_stuck": (2, 60),
        "anomaly": (2, settings.anomaly_dedup_s),
    }


# ---------------------------------------------------------------- 2. motor_temp

def test_motor_temp_needs_three_consecutive_samples():
    """motor_temp: triggers only on >75°C AND **≥3 consecutive samples**; fewer than 3 does not trigger."""
    rule = MotorTempRule()

    hot = payload(temp=82.0)
    w = deque(maxlen=settings.window_maxlen)

    w.append(payload(temp=82.0))
    w.append(payload(temp=82.0))
    assert rule.evaluate(hot, w) is None, "only 2 samples should not trigger (requires ≥3)"

    w.append(hot)
    alert = rule.evaluate(hot, w)
    assert alert is not None and alert.rule_id == "motor_temp"
    assert alert.severity == 3
    assert alert.detail["temp"] == 82.0


def test_motor_temp_threshold_is_strict_and_resets_on_gap():
    """Exactly 75°C does **not** trigger (spec is `>` not `>=`); a low-temp point in the middle
    must restart counting."""
    rule = MotorTempRule()
    w = deque(maxlen=settings.window_maxlen)

    at_threshold = payload(temp=75.0)
    for _ in range(3):
        w.append(at_threshold)
    assert rule.evaluate(at_threshold, w) is None, "75°C must not trigger (strictly greater than threshold)"

    w.clear()
    w.append(payload(temp=80.0))
    w.append(payload(temp=30.0))          # dips back to normal temperature in the middle
    w.append(payload(temp=80.0))
    assert rule.evaluate(payload(temp=80.0), w) is None, \
        "a normal temp among the last 3 means it's not 'sustained' overheating"


# ---------------------------------------------------------------- 3. battery_low

def test_battery_low_triggers_below_threshold_only():
    """battery_low: triggers below 15% (severity 2 / dedup 120s); ≥15% does not trigger."""
    rule = BatteryLowRule()
    w = deque(maxlen=settings.window_maxlen)

    assert rule.evaluate(payload(batt=15.0), w) is None, "15.0% does not trigger (strictly less than)"
    assert rule.evaluate(payload(batt=40.0), w) is None

    alert = rule.evaluate(payload(batt=9.3), w)
    assert alert is not None
    assert (alert.rule_id, alert.severity) == ("battery_low", 2)
    assert BatteryLowRule().dedup_window_s == 120
    assert alert.detail["battery_pct"] == 9.3


def test_battery_low_ignores_missing_field():
    """Missing fields must not raise (when the backend schema drifts the engine must survive, not
    crash the whole consumption line)."""
    rule = BatteryLowRule()
    assert rule.evaluate({"robot_id": "humanoid-001"}, deque()) is None


# ---------------------------------------------------------------- 4. joint_stuck

def test_joint_stuck_requires_still_angle_but_high_torque():
    """joint_stuck: within a 5s window the angle variance≈0 and mean torque exceeds threshold; once
    it moves it's not stuck."""
    rule = JointStuckRule()
    n = rule.need_samples
    assert n == 50, "5s × 10Hz should be 50 samples"

    # (a) angle completely still + torque 10 > 8.0 → stuck
    w = deque((payload(angle=0.7, torque=10.0) for _ in range(n)),
              maxlen=settings.window_maxlen)
    alert = rule.evaluate(payload(angle=0.7, torque=10.0), w)
    assert alert is not None and alert.rule_id == "joint_stuck"
    assert alert.severity == 2
    assert alert.detail["variance"] == 0.0

    # (b) angle moving (variance far above 1e-6) → no trigger
    import math
    w2 = deque((payload(angle=math.sin(i), torque=10.0) for i in range(n)),
               maxlen=settings.window_maxlen)
    assert rule.evaluate(payload(angle=0.0, torque=10.0), w2) is None

    # (c) angle still but torque below threshold (not exerting force, maybe just parked) → no trigger
    w3 = deque((payload(angle=0.7, torque=3.0) for _ in range(n)),
               maxlen=settings.window_maxlen)
    assert rule.evaluate(payload(angle=0.7, torque=3.0), w3) is None


# ---------------------------------------------------------------- 5. offline

def test_offline_rule_semantics():
    """offline: no telemetry for >10s triggers; never reported (last_seen=None) also triggers."""
    rule = OfflineRule()

    assert rule.evaluate({"robot_id": "humanoid-001", "last_seen": time.time()},
                         deque()) is None, "just reported should not be judged offline"

    fresh = rule.evaluate({"robot_id": "humanoid-001",
                           "last_seen": time.time() - 11.0}, deque())
    assert fresh is not None and fresh.severity == 3
    assert fresh.detail["age_s"] >= 10.0

    never = rule.evaluate({"robot_id": "humanoid-002", "last_seen": None}, deque())
    assert never is not None, "never-online must also be judgeable as offline"


def test_offline_has_no_redis_expiry_dependency():
    """Acceptance criterion 2: the code must **not** depend on Redis key-expiry events.

    Static assertion + real-shape assertion, double-checked:
      1) the offline rule and main's judgement path contain no keyspace/expired related API;
      2) the offline rule does **not** touch Redis (get_redis is not visible in app.rules.offline)."""
    import ast
    import inspect
    import textwrap

    import app.rules.offline as offline_mod
    import app.main as main_mod

    def code_only(mod) -> str:
        """Keep only executable code: strip docstrings and `#` comments.

        Necessity: `rules/offline.py` and `main.ticker()` **deliberately write in their comments**
        "notify-keyspace-events is off by default" to explain why expiry events aren't used;
        scanning the raw text would misjudge "why it's disabled" as "it depends on it"."""
        src = inspect.getsource(mod)
        tree = ast.parse(textwrap.dedent(src))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
        out = ast.unparse(tree)
        return "\n".join(ln.split("#", 1)[0] for ln in out.splitlines())

    src = code_only(offline_mod) + "\n" + code_only(main_mod.ticker)
    for forbidden in ("notify-keyspace-events", "keyspace", "__keyevent@",
                      "expired", "get_redis", "psubscribe"):
        assert forbidden not in src, f"offline judgement path has a Redis key-expiry dependency: {forbidden}"

    assert not hasattr(offline_mod, "get_redis"), "offline rule should not connect to Redis itself"


def test_offline_ticker_only_uses_in_memory_last_seen(engine, monkeypatch):
    """Run one real ticker round: in-memory last_seen expiry → offline; resume reporting → no more.

    Here we **don't mock the rule judgement**, only swap emit's exit (save) and DB update for
    recorders, asserting the ticker's judgement and online-state machine itself."""
    import app.main as m

    saved: list = []

    async def fake_save(alert):
        saved.append(alert)

    async def fake_update_online(rid, online):
        return None

    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(m, "update_online", fake_update_online)
    # Keep only one robot, to avoid the whole-table 100-robot per-round spawn of persistence tasks.
    # settings.robot_ids is a read-only property (config.py:robot_ids) and can't be setattr;
    # but ticker reads `settings.robot_ids`, so replace the writable shadow of that attribute on settings.
    monkeypatch.setattr(type(m.settings), "robot_ids",
                        property(lambda self: ["humanoid-001"]))
    # Drive round by round manually: ticker is `while True: sleep(interval); <body>`;
    # if interval=0 it spins furiously, emitting hundreds of alerts and losing assertion meaning.
    # Here set interval "longer than one sleep" and use one sleep to release **exactly one round**.
    monkeypatch.setattr(m.settings, "tick_interval_s", 0.2)

    async def one_round():
        import asyncio as _a
        task = _a.create_task(m.ticker())
        await _a.sleep(0.3)      # interval 0.2 → only one round completes, the 2nd is still sleeping
        task.cancel()
        try:
            await task
        except _a.CancelledError:
            pass
        if m._tasks:             # release the spawned persistence/update tasks
            await _a.gather(*list(m._tasks), return_exceptions=True)

    # (a) reported 11s ago → judged offline this round
    engine.last_seen["humanoid-001"] = time.time() - 11.0
    engine.online_state["humanoid-001"] = True
    run(one_round())
    assert [a.rule_id for a in saved] == ["offline"]
    assert engine.online_state["humanoid-001"] is False, "ticker must maintain online state in sync"

    # (b) resume reporting (last_seen=now) → no more offline alert
    saved.clear()
    engine.last_seen["humanoid-001"] = time.time()
    run(one_round())
    assert saved == [], "after resume reporting there should be no more offline alert"
    assert engine.online_state["humanoid-001"] is True


# ---------------------------------------------------------------- 6. dedup idempotence

class _FakeRedis:
    """Thin wrapper over fakeredis: only exposes the `set(nx=..., ex=...)` this case really uses."""

    def __init__(self):
        self._r = fakeredis.FakeRedis(decode_responses=True)

    async def set(self, key, value, ex=None, nx=False):
        return self._r.set(key, value, ex=ex, nx=nx)


@pytest.fixture()
def fake_redis(monkeypatch):
    client = _FakeRedis()

    async def fake_get_redis():
        return client

    monkeypatch.setattr(deduplicator, "get_redis", fake_get_redis)
    return client


def test_dedup_is_idempotent_within_window(fake_redis):
    """Acceptance criterion 3: repeated messages within the same window pass through only once
    (no duplicate writes)."""
    alert = MotorTempRule().evaluate(
        payload(temp=82.0),
        deque((payload(temp=82.0) for _ in range(3)), maxlen=settings.window_maxlen))

    window = MotorTempRule().dedup_window_s
    first = run(deduplicator.should_emit(alert, window))
    second = run(deduplicator.should_emit(alert, window))
    third = run(deduplicator.should_emit(alert, window))

    assert [first, second, third] == [True, False, False], \
        "repeated messages within the window must pass only the first one"


def test_dedup_key_shape_and_ttl(fake_redis):
    """Dedup key shape `alert:dedup:{robot_id}:{rule_id}`, TTL = window seconds."""
    alert = BatteryLowRule().evaluate(payload(batt=9.0), deque())
    run(deduplicator.should_emit(alert, BatteryLowRule().dedup_window_s))

    key = "alert:dedup:humanoid-001:battery_low"
    assert fake_redis._r.exists(key) == 1
    ttl = fake_redis._r.ttl(key)
    assert 0 < ttl <= 120, f"TTL should be the dedup window (120s), actual {ttl}"


def test_dedup_is_per_robot_and_per_rule(fake_redis):
    """Dedup granularity is (robot_id, rule_id): a different robot / a different rule must each pass."""
    a1 = BatteryLowRule().evaluate(payload("humanoid-001", batt=9.0), deque())
    a2 = BatteryLowRule().evaluate(payload("humanoid-002", batt=9.0), deque())
    a3 = MotorTempRule().evaluate(
        payload("humanoid-001", temp=82.0),
        deque((payload("humanoid-001", temp=82.0) for _ in range(3)),
              maxlen=settings.window_maxlen))

    assert run(deduplicator.should_emit(a1, 120)) is True
    assert run(deduplicator.should_emit(a2, 120)) is True, "different robots must not suppress each other"
    assert run(deduplicator.should_emit(a3, 60)) is True, "different rules must not suppress each other"


def test_dedup_fails_open_when_redis_down(monkeypatch):
    """When Redis is unreachable, fail-open (pass through) — rather make noise than silently drop a
    real fault."""
    async def boom():
        raise ConnectionError("injected: redis down")

    monkeypatch.setattr(deduplicator, "get_redis", boom)
    alert = BatteryLowRule().evaluate(payload(batt=9.0), deque())
    assert run(deduplicator.should_emit(alert, 120)) is True


# ---------------------------------------------------------------- 7. persist + publish

class _RecordingConn:
    """Records the INSERT SQL and argument sequence, returns an alert_id."""

    def __init__(self):
        self.calls: list = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 4242


class _RecordingPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AsyncCtx(self._conn)


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class _RecordingRedis:
    def __init__(self):
        self.published: list = []

    async def publish(self, chan, payload):
        self.published.append((chan, payload))


def test_save_writes_robot_alert_and_publishes(monkeypatch):
    """Acceptance criterion 4: write to `robot_alert` and PUBLISH to `robot:alerts` takes effect.

    Assertions go down to SQL text and argument sequence: table name, column order, `::jsonb`
    cast, RETURNING alert_id all checked item-by-item against `app/store.py` and
    `infra/init.sql` column definitions."""
    import json
    from app.rules.base import Alert

    conn = _RecordingConn()
    redis = _RecordingRedis()

    async def fake_get_pool():
        return _RecordingPool(conn)

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(store_mod, "get_pool", fake_get_pool)
    monkeypatch.setattr(store_mod, "get_redis", fake_get_redis)

    alert = Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                  message="Motor over-temperature", detail={"joint": "head_yaw", "temp": 82.0})
    run(store_mod.save(alert))

    # (1) persist: one INSERT INTO robot_alert, 6 params, detail via jsonb
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO robot_alert" in sql
    assert "RETURNING alert_id" in sql
    assert "$6::jsonb" in sql, "detail must be explicitly cast to jsonb (otherwise asyncpg can't infer the type)"
    assert args[0].tzinfo is not None, "time must be a timezone-aware UTC timestamp"
    assert args[1:6] == ("humanoid-001", "motor_temp", 3, "Motor over-temperature",
                         json.dumps({"joint": "head_yaw", "temp": 82.0}))

    # (2) publish: channel and envelope fields
    assert len(redis.published) == 1
    chan, raw = redis.published[0]
    assert chan == "robot:alerts"
    env = json.loads(raw)
    assert env["type"] if "type" in env else True      # envelope is flat JSON (no type field is legacy)
    for field in ("robot_id", "rule_id", "severity", "message", "detail", "ts"):
        assert field in env, f"envelope missing field {field}"
    assert env["robot_id"] == "humanoid-001"
    assert env["severity"] == 3
    # alert_id comes from RETURNING; the frontend "ack" button addresses it
    assert env["alert_id"] == 4242


def test_save_publishes_even_if_insert_fails(monkeypatch):
    """A failed write must still publish: the frontend alert stream is the only visible surface
    (the two try blocks are independent, see the top of store.py)."""
    from app.rules.base import Alert

    class _BrokenPool:
        def acquire(self):
            raise ConnectionError("injected: pool exhausted")

    redis = _RecordingRedis()

    async def fake_get_pool():
        return _BrokenPool()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(store_mod, "get_pool", fake_get_pool)
    monkeypatch.setattr(store_mod, "get_redis", fake_get_redis)

    run(store_mod.save(Alert(robot_id="humanoid-001", rule_id="battery_low",
                             severity=2, message="Low battery", detail={"battery_pct": 9.0})))

    assert len(redis.published) == 1, "a failed write must not choke the frontend alert stream"
    assert redis.published[0][0] == "robot:alerts"


# ---------------------------------------------------------------- 8. consumption line → persist

def test_consume_line_feeds_window_and_emits(engine, monkeypatch):
    """Consumption line: one real payload into the window → three stream rules run once → a hit goes to emit.

    Use 3 overheating payloads to simulate consecutive sampling, verifying `consume()`'s window
    maintenance and rule scheduling.

    Note (after Order 30): this case asserts the **rule scheduling and window**, not inhibition
    behavior. The 3rd payload hits both motor_temp(sev3) and battery_low(sev2); Order 30's severity
    inhibition would correctly suppress battery_low — that's a different layer's contract (see
    test_severity_inhibition.py). So here we explicitly disable severity inhibition, keeping only
    the pure scheduling path, to avoid using "inhibition side-effect" to test "was the rule run"."""
    import app.main as m
    from app.config import settings
    monkeypatch.setattr(settings, "inhibit_severity", False)

    saved: list = []

    async def fake_save(alert):
        saved.append(alert)

    async def fake_should_emit(alert, window_s):
        return True

    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(m, "should_emit", fake_should_emit)

    # Feed the consumption line's "single-item processing" logic directly: swap pubsub.listen() for a fixed sequence.
    #
    # ⚠️ After R-14 this fake must **faithfully match the real driver**: real redis-py's `listen()`
    # never ends naturally (it only throws on disconnect). Before R-14 consume() was a bare
    # `async for`; a fake that "returns after yielding" let consume() return along — this case
    # piggybacked on that side effect to finish. Now consume() is wrapped in `while True`
    # (the necessary cost of disconnect self-heal); if the fake "ends naturally" again it's
    # equivalent to making consume() re-subscribe every round, and this case would run forever
    # (empirically 30s timeout). So here: after yielding 3 messages, hang, and let drive()
    # cancel to finish — the assertion target (window contents + which rules fired) is **unchanged**
    # from before.
    class _PubSub:
        def __init__(self):
            self._msgs = [
                {"type": "subscribe"},
                {"type": "message",
                 "data": __import__("json").dumps(payload(temp=82.0))},
                {"type": "message",
                 "data": __import__("json").dumps(payload(temp=83.0))},
                {"type": "message",
                 "data": __import__("json").dumps(payload(temp=84.0, batt=9.0))},
            ]

        async def subscribe(self, chan):
            return None

        async def listen(self):
            for msg in self._msgs:
                yield msg
            # After yielding messages → never returns (real-driver property: connection still up, just no message yet)
            await __import__("asyncio").Event().wait()

    class _R:
        def pubsub(self):
            return _PubSub()

    async def fake_get_redis():
        return _R()

    monkeypatch.setattr(m, "get_redis", fake_get_redis)

    async def drive():
        # Wait until 3 messages are in the window (the consumption line is a coroutine; you can't await it to finish itself)
        task = __import__("asyncio").ensure_future(m.consume())
        for _ in range(200):
            await __import__("asyncio").sleep(0)
            if "humanoid-001" in m.windows and len(m.windows["humanoid-001"]) == 3:
                break
        task.cancel()
        try:
            await task
        except __import__("asyncio").CancelledError:
            pass
        # consume() internally uses spawn() to throw emit into a background task; wait for them to finish
        if m._tasks:
            await __import__("asyncio").gather(*list(m._tasks),
                                              return_exceptions=True)

    run(drive())

    rule_ids = sorted({a.rule_id for a in saved})
    assert "motor_temp" in rule_ids, "3 consecutive overheating samples should trigger motor_temp"
    assert "battery_low" in rule_ids, "the 3rd payload's battery 9% should trigger battery_low"
    assert m.windows["humanoid-001"].__len__() == 3
