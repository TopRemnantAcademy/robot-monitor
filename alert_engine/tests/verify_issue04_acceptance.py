#!/usr/bin/env python3
"""订单 04（Issue #4）端到端验收脚本 —— 无 Docker 环境下的**真实现**验证。

为什么做这个脚本（而不是只跑 pytest）
------------------------------------
本环境没有 Docker，`test_integration.sh` 无法起 EMQX/TimescaleDB/Redis，
所以派工单里「模拟器跑 30s+ 出 motor_temp/battery_low」「停掉一台 10s 内出 offline」
这两条**在容器链路里**暂时验不了。但这两条的**引擎侧逻辑**是可以在本机完整驱动的：

  mock_fleet 的 payload 生成 → Redis Pub/Sub(robot:stream:all) → main.consume()
      → 4 条规则 → dedup → store（假连接记录 SQL）→ PUBLISH robot:alerts

即：**全链路只把三个外部依赖换成进程内替身**（Redis→fakeredis、
PG 连接池→记录器、MQTT broker→mock_fleet 的 RobotState 直接调用），
`main.consume` / `ticker` / 4 条规则 / `deduplicator` / `store` 全是真代码。

跑法（在 alert_engine/ 目录）：
    python3 tests/verify_issue04_acceptance.py

验收项（对照 orders/04-alert-engine.md §验收标准）：
    V1  4 个规则类齐全且继承 Rule 基类
    V2  offline 判定路径无 Redis 过期事件依赖（AST 剥注释后扫描）
    V3  去重器幂等（重复消息不重复写库）
    V4  写入 robot_alert 且 PUBLISH robot:alerts（断言 SQL 文本与频道）
    V5  模拟器 30s+（加速）→ 同时出现 motor_temp 与 battery_low
    V6  停掉某台（不再投递遥测）→ 10s 内出现 offline
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
    sys.path.insert(0, str(REPO / "simulator"))   # mock_fleet.py 所在目录

import fakeredis                                   # noqa: E402

from app import deduplicator                       # noqa: E402
from app import store as store_mod                 # noqa: E402
from app.rules.base import Rule                    # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(tag: str, ok: bool, detail: str) -> None:
    results.append((tag, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {tag}  {detail}")


# --------------------------------------------------------------- 替身

class FakeRedis:
    """fakeredis 包装：暴露 pubsub / set / publish / ttl / exists。"""

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
        # silence.is_silenced 用 MGET 一次读「全局 Key + 单机 Key」；
        # 缺这个方法会走 fail-open 并打 ERROR 堆栈（噪音），补上即真实语义。
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
    """把 4 个模块的 get_redis / get_pool 换成替身（真代码不动）。"""
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

    # 让 consume() 拿到替身
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


# --------------------------------------------------------------- V1/V2 静态

def v1_rules() -> None:
    import app.main as m
    ids = sorted(r.rule_id for r in m.RULES)
    ok = ids == ["battery_low", "joint_stuck", "motor_temp", "offline"] and \
        all(isinstance(r, Rule) for r in m.RULES)
    check("V1", ok, f"RULES={ids}，全部继承 Rule；"
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
    check("V2", not banned, f"offline 判定路径（AST 去注释）禁用词命中={banned or '无'}")


# --------------------------------------------------------------- V3/V4 去重+落库

def v3_dedup_idempotent(redis: FakeRedis) -> None:
    from app.rules.base import Alert
    alert = Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                  message="过热", detail={"temp": 82.0})

    async def go():
        return [await deduplicator.should_emit(alert, 60) for _ in range(3)]

    got = asyncio.run(go())
    key = "alert:dedup:humanoid-001:motor_temp"
    ttl = redis.ttl(key)
    ok = got == [True, False, False] and 0 < ttl <= 60 and redis.exists(key) == 1
    check("V3", ok, f"同窗口 3 次 should_emit={got}；Key={key} ttl={ttl}s")


def v4_insert_and_publish(redis: FakeRedis, inserts: list) -> None:
    from app.rules.base import Alert

    async def go():
        await store_mod.save(Alert(robot_id="humanoid-001", rule_id="battery_low",
                                   severity=2, message="低电量", detail={"pct": 9.0}))

    asyncio.run(go())
    sql, args = inserts[-1]
    chan, raw = redis.published[-1]
    env = json.loads(raw)
    ok = ("INSERT INTO robot_alert" in sql and "RETURNING alert_id" in sql
          and "$6::jsonb" in sql and chan == "robot:alerts"
          and env["robot_id"] == "humanoid-001" and env["alert_id"] is not None
          and set(("robot_id", "rule_id", "severity", "message", "detail", "ts",
                   "alert_id", "escalated")) <= set(env))
    check("V4", ok, f"SQL 命中 robot_alert+RUNNING alert_id；PUBLISH {chan} "
                    f"信封键={sorted(env)}；alert_id={env['alert_id']}")


# --------------------------------------------------------------- V5/V6 端到端

def build_stream_payloads(seconds: int = 45) -> list[dict]:
    """用**模拟器真身**的 `RobotState` 生成 32s 的 10Hz 遥测（本地加速，不睡觉）。

    只取 humanoid-001 一台，注入过热关节；电量按 mock_fleet 的
    BATTERY_DRAIN_PER_S=0.05%/s 演化，并把初值压到 15% 附近以在 30s 内越线。

    时长取 45s 而不是 30s：模拟器 `OVERHEAT_AFTER_S=30` 之后才升温，且每周期
    至多 +0.6°C，实测（随机种子 0）到第 ~317 个采样点（31.7s）才越过 75°C，
    再过 3 个采样点才满足 motor_temp 的「连续 ≥3 点」。45s 留出足够余量。
    """
    import random

    import mock_fleet as mf

    random.seed(0)                             # 钉死随机游走，验收结果可复现
    st = mf.RobotState(robot_id="humanoid-001")
    st.battery_pct = 15.4                      # 15.4 - 0.05*45 ≈ 13.2 → 30s 内跌破 15%
    st.overheating = True
    st.overheat_joint = mf.JOINT_NAMES[9]

    t0 = 1000.0                                 # 用假时钟：start_at 后 30s 才升温
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
          f"{len(payloads)} 条遥测（45s×10Hz）喂入 consume()：命中规则={kinds}，"
          f"落库={len(inserts)} 条，PUBLISH={len(redis.published)} 次")
    if not v5_ok:
        print("     未命中的规则说明：", [k for k in ("motor_temp", "battery_low")
                                          if k not in kinds])

    # V6：停掉 humanoid-001（清空后续投递），把 last_seen 拨回 11s 前，跑真 ticker
    v6_ok = False
    detail = ""
    m.windows.clear()
    m.online_state.clear()
    m.last_seen["humanoid-001"] = time.time() - 11.0     # 11s 无遥测
    # settings.robot_ids 是只读 property（config.py:robot_ids），不可赋值；
    # 但 ticker 读的就是 `settings.robot_ids`，故把类上的属性临时换成常量列表。
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
    detail = (f"last_seen 拨回 11s 前，真 ticker 一轮 → 规则={sorted(set(tick_emitted))}；"
              f"online_state={m.online_state.get('humanoid-001')}")
    check("V6", v6_ok, detail)
    # 恢复上报后不应再出
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
          f"恢复上报后一轮 → 规则={sorted(set(tick_emitted))}（应为空）")


# --------------------------------------------------------------- main

def main() -> int:
    print("== 订单 04（Issue #4）验收：对照 orders/04-alert-engine.md ==")
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
    print(f"\n== 结论：{passed}/{total} 通过 ==")
    for tag, st, _ in results:
        if st == FAIL:
            print(f"  未通过：{tag}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
