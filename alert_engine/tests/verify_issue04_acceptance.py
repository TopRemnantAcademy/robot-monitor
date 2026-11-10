#!/usr/bin/env python3
"""Order 04 (Issue #4) end-to-end acceptance script — **real** verification in a no-Docker environment.

Why this script (instead of only running pytest)
------------------------------------
This environment has no Docker, so `test_integration.sh` can't bring up EMQX/TimescaleDB/Redis,
and the two acceptance items "simulator runs 30s+ and emits motor_temp/battery_low" and
"stop one machine and get offline within 10s" can't be verified **in the container chain** for
now. But the **engine-side logic** of those two items can be fully driven on this machine:

  mock_fleet's payload generation → Redis Pub/Sub (robot:stream:all) → main.consume()
      → 4 rules → dedup → store (fake connection records SQL) → PUBLISH robot:alerts

That is: **the full chain only swaps three external dependencies for in-process stand-ins**
(Redis→fakeredis, PG connection pool→recorder, MQTT broker→mock_fleet's RobotState called
directly); `main.consume` / `ticker` / 4 rules / `deduplicator` / `store` are all real code.

Run (from the alert_engine/ directory):
    python3 tests/verify_issue04_acceptance.py

Acceptance items (against orders/04-alert-engine.md §acceptance criteria):
    V1  4 rule classes present and inherit the Rule base
    V2  offline judgement path has no Redis key-expiry dependency (AST with comments stripped)
    V3  deduplicator is idempotent (repeated messages don't double-write to DB)
    V4  writes robot_alert and PUBLISHes robot:alerts (assert SQL text and channel)
    V5  simulator 30s+ (accelerated) → both motor_temp and battery_low appear
    V6  stop one machine (no more telemetry delivered) → offline within 10s
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]        # alert_engine/
REPO = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "simulator") not in sys.path:
    sys.path.insert(0, str(REPO / "simulator"))   # directory containing mock_fleet.py

import fakeredis                                   # noqa: E402

from app import deduplicator                       # noqa: E402
from app import store as store_mod                 # noqa: E402
from app.rules.base import Rule                    # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(tag: str, ok: bool, detail: str) -> None:
    results.append((tag, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {tag}  {detail}")


# --------------------------------------------------------------- stand-ins

class FakeRedis:
    """fakeredis wrapper: exposes pubsub / set / publish / ttl / exists."""

    def __init__(self):
        self._r = fakeredis.FakeRedis(decode_responses=True)
        self.published: list[tuple[str, str]] = []
        self._subs: dict[str, list] = {}

    def pubsub(self):
        outer = self

        class _PS:
            async def subscribe(self, chan):
                outer._subs.setdefault(chan, [])

            async def listen(self):
                for msg in outer._subs.get("robot:stream:all", []):
                    yield msg

        return _PS()

    async def set(self, key, value, ex=None, nx=False):
        return self._r.set(key, value, ex=ex, nx=nx)

    async def publish(self, chan, payload):
        self.published.append((chan, payload))

    def exists(self, key):
        return self._r.exists(key)

    def ttl(self, key):
        return self._r.ttl(key)

    async def mget(self, *keys):
        # silence.is_silenced uses MGET to read "global key + per-machine key" in one shot;
        # without this method it would take the fail-open path and print an ERROR stacktrace
        # (noise) — adding it matches the real semantics.
        return self._r.mget(*keys)


class RecordingConn:
    def __init__(self, sink: list, counter: dict):
        self._sink, self._counter = sink, counter

    async def fetchval(self, sql, *args):
        self._counter["n"] += 1
        self._sink.append((sql, args))
        return 1000 + self._counter["n"]

    async def execute(self, sql, *args):
        self._counter["n"] += 1
        self._sink.append((sql, args))
        return "UPDATE 1"


class RecordingPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def wire(monkeypatch_like: dict) -> tuple[FakeRedis, list]:
    """Swap the 4 modules' get_redis / get_pool for stand-ins (real code untouched)."""
    import app.db as db
    import app.main as m
    import app.silence as silence_mod

    redis = FakeRedis()
    inserts: list = []
    counter = {"n": 0}
    conn = RecordingConn(inserts, counter)

    async def fake_redis():
        return redis

    async def fake_pool():
        return RecordingPool(conn)

    for mod in (db, m, store_mod, silence_mod, deduplicator):
        if hasattr(mod, "get_redis"):
            monkeypatch_like[mod] = mod.get_redis
            mod.get_redis = fake_redis
    monkeypatch_like[store_mod] = getattr(store_mod, "get_pool", None)
    store_mod.get_pool = fake_pool

    # Make consume() get the stand-in
    m.get_redis = fake_redis
    return redis, inserts


def unwire(saved: dict) -> None:
    import app.main as m
    import app.silence as silence_mod
    import app.db as db

    for mod in (db, m, store_mod, silence_mod, deduplicator):
        if mod in saved and saved[mod] is not None:
            mod.get_redis = saved[mod]
    if store_mod in saved and saved[store_mod] is not None:
        store_mod.get_pool = saved[store_mod]


# --------------------------------------------------------------- V1/V2 static

def v1_rules() -> None:
    import app.main as m
    ids = sorted(r.rule_id for r in m.RULES)
    ok = ids == ["battery_low", "joint_stuck", "motor_temp", "offline"] and \
        all(isinstance(r, Rule) for r in m.RULES)
    check("V1", ok, f"RULES={ids}, all inherit Rule; "
                    f"severity/window=" +
          str({r.rule_id: (r.severity, r.dedup_window_s) for r in m.RULES}))


def v2_no_redis_expiry() -> None:
    import ast
    import inspect
    import textwrap

    import app.main as m
    import app.rules.offline as off

    def code_only(mod) -> str:
        tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                b = node.body
                if b and isinstance(b[0], ast.Expr) and \
                        isinstance(b[0].value, ast.Constant) and \
                        isinstance(b[0].value.value, str):
                    node.body = b[1:] or [ast.Pass()]
        return "\n".join(ln.split("#", 1)[0] for ln in ast.unparse(tree).splitlines())

    src = code_only(off) + "\n" + code_only(m.ticker)
    banned = [t for t in ("notify-keyspace-events", "keyspace", "__keyevent@",
                          "expired", "get_redis", "psubscribe") if t in src]
    check("V2", not banned, f"offline judgement path (AST comments stripped) banned-word hits={banned or 'none'}")


# --------------------------------------------------------------- V3/V4 dedup + persist

def v3_dedup_idempotent(redis: FakeRedis) -> None:
    from app.rules.base import Alert
    alert = Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                  message="Overheating", detail={"temp": 82.0})

    async def go():
        return [await deduplicator.should_emit(alert, 60) for _ in range(3)]

    got = asyncio.run(go())
    key = "alert:dedup:humanoid-001:motor_temp"
    ttl = redis.ttl(key)
    ok = got == [True, False, False] and 0 < ttl <= 60 and redis.exists(key) == 1
    check("V3", ok, f"same-window 3x should_emit={got}; Key={key} ttl={ttl}s")


def v4_insert_and_publish(redis: FakeRedis, inserts: list) -> None:
    from app.rules.base import Alert

    async def go():
        await store_mod.save(Alert(robot_id="humanoid-001", rule_id="battery_low",
                                   severity=2, message="Low battery", detail={"pct": 9.0}))

    asyncio.run(go())
    sql, args = inserts[-1]
    chan, raw = redis.published[-1]
    env = json.loads(raw)
    ok = ("INSERT INTO robot_alert" in sql and "RETURNING alert_id" in sql
          and "$6::jsonb" in sql and chan == "robot:alerts"
          and env["robot_id"] == "humanoid-001" and env["alert_id"] is not None
          and set(("robot_id", "rule_id", "severity", "message", "detail", "ts",
                   "alert_id", "escalated")) <= set(env))
    check("V4", ok, f"SQL hits robot_alert + RETURNING alert_id; PUBLISH {chan} "
                    f"envelope keys={sorted(env)}; alert_id={env['alert_id']}")


# --------------------------------------------------------------- V5/V6 end-to-end

def build_stream_payloads(seconds: int = 45) -> list[dict]:
    """Use the **real** simulator `RobotState` to generate 32s of 10Hz telemetry (locally
    accelerated, no sleeping).

    Only take humanoid-001, inject an overheating joint; battery evolves per mock_fleet's
    BATTERY_DRAIN_PER_S=0.05%/s, with the initial value pressed down near 15% to cross the line
    within 30s.

    Duration is 45s not 30s: the simulator only heats up after OVERHEAT_AFTER_S=30, and each cycle
    rises at most +0.6°C; empirically (random seed 0) it takes until ~sample 317 (31.7s) to cross
    75°C, then 3 more samples to satisfy motor_temp's "≥3 consecutive". 45s leaves enough margin."""
    import random

    import mock_fleet as mf

    random.seed(0)                             # pin the random walk so the acceptance result is reproducible
    st = mf.RobotState(robot_id="humanoid-001")
    st.battery_pct = 15.4                      # 15.4 - 0.05*45 ≈ 13.2 → crosses 15% within 30s
    st.overheating = True
    st.overheat_joint = mf.JOINT_NAMES[9]

    t0 = 1000.0                                 # fake clock: heats up 30s after start_at
    out = []
    for i in range(seconds * 10):
        now = t0 + i * 0.1
        st.step(now, t0)
        out.append(st.build_payload(int(now * 1000)))
    return out


def v56_end_to_end(saved: dict, redis: FakeRedis, inserts: list) -> None:
    import app.main as m

    payloads = build_stream_payloads(45)
    for p in payloads:
        redis._subs.setdefault("robot:stream:all", []).append(
            {"type": "message", "data": json.dumps(p)})

    emitted: list[str] = []
    real_emit = m.emit

    async def spy_emit(alert):
        emitted.append(alert.rule_id)
        await real_emit(alert)

    m.emit = spy_emit

    async def go():
        await m.consume()
        if m._tasks:
            await asyncio.gather(*list(m._tasks), return_exceptions=True)

    try:
        asyncio.run(go())
    finally:
        m.emit = real_emit

    kinds = sorted(set(emitted))
    v5_ok = "motor_temp" in kinds and "battery_low" in kinds
    check("V5", v5_ok,
          f"{len(payloads)} telemetry records (45s×10Hz) fed into consume(): rules hit={kinds}, "
          f"persisted={len(inserts)} rows, PUBLISH={len(redis.published)} times")
    if not v5_ok:
        print("     rules not hit:", [k for k in ("motor_temp", "battery_low")
                                      if k not in kinds])

    # V6: stop humanoid-001 (clear subsequent delivery), dial last_seen back 11s, run the real ticker
    v6_ok = False
    detail = ""
    m.windows.clear()
    m.online_state.clear()
    m.last_seen["humanoid-001"] = time.time() - 11.0     # 11s without telemetry
    # settings.robot_ids is a read-only property (config.py:robot_ids) and can't be assigned;
    # but ticker reads exactly `settings.robot_ids`, so swap the property on the class for a constant list.
    _orig_ids = type(m.settings).robot_ids
    type(m.settings).robot_ids = property(lambda self: ["humanoid-001"])

    tick_emitted: list[str] = []

    async def spy2(alert):
        tick_emitted.append(alert.rule_id)
        await real_emit(alert)

    m.emit = spy2
    interval = m.settings.tick_interval_s
    m.settings.tick_interval_s = 0.2

    async def tick_round():
        t = asyncio.create_task(m.ticker())
        await asyncio.sleep(0.3)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        if m._tasks:
            await asyncio.gather(*list(m._tasks), return_exceptions=True)

    try:
        asyncio.run(tick_round())
    finally:
        m.emit = real_emit
        m.settings.tick_interval_s = interval
        type(m.settings).robot_ids = _orig_ids

    v6_ok = "offline" in tick_emitted
    detail = (f"last_seen dialed back 11s, one real ticker round → rules={sorted(set(tick_emitted))}; "
              f"online_state={m.online_state.get('humanoid-001')}")
    check("V6", v6_ok, detail)
    # After recovery report, should not fire again
    m.last_seen["humanoid-001"] = time.time()
    tick_emitted.clear()
    m.emit = spy2
    m.settings.tick_interval_s = 0.2
    type(m.settings).robot_ids = property(lambda self: ["humanoid-001"])
    try:
        asyncio.run(tick_round())
    finally:
        m.emit = real_emit
        m.settings.tick_interval_s = interval
        type(m.settings).robot_ids = _orig_ids
    check("V6b", tick_emitted == [],
          f"after recovery report one round → rules={sorted(set(tick_emitted))} (should be empty)")


# --------------------------------------------------------------- main

def main() -> int:
    print("== Order 04 (Issue #4) acceptance: against orders/04-alert-engine.md ==")
    saved: dict = {}
    try:
        redis, inserts = wire(saved)
        v1_rules()
        v2_no_redis_expiry()
        v3_dedup_idempotent(redis)
        v4_insert_and_publish(redis, inserts)
        v56_end_to_end(saved, redis, inserts)
    finally:
        unwire(saved)

    passed = sum(1 for _, s, _ in results if s == PASS)
    total = len(results)
    print(f"\n== Conclusion: {passed}/{total} passed ==")
    for tag, st, _ in results:
        if st == FAIL:
            print(f"  not passed: {tag}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
