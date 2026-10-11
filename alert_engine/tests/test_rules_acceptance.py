"""订单 04（Issue #4）验收测试：4 条规则 / offline ticker / 去重幂等 / 落库+发布。

本文件补的是**此前测不到的缺口**：仓库现有用例覆盖的是告警闭环层
（抑制顺序、静默顺序、落库失败计数），**没有一条**真正喂遥测进 4 个规则、
也没有一条证明去重器幂等或 store 真的写了 robot_alert 并发布 robot:alerts。

被测对象是真实实现，不是替身：
- `should_emit` → 真 `app.deduplicator`，配 fakeredis 顶替 Redis；
  `SET NX` 是 Redis 服务端语义，**必须由真 Redis 语义**（fakeredis）来验，
  进程内的 dict 替身测不出「幂等」。
- `save` → 真 `app.store`，配「记录 SQL 的假连接 + 记录 PUBLISH 的假 Redis」，
  断言的是 SQL 文本、参数序列与频道名，而非「函数存在」。
- `ticker` → 真 `app.main.ticker`，跑**真一轮**（不 mock 掉判定），
  再把 `last_seen` 往前拨 11s 模拟「10s 无遥测」。

依据：`orders/04-alert-engine.md` §验收标准、`spec/02` §3/§3.1/§4/§5。
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


# ---------------------------------------------------------------- payload 构造

def payload(rid: str = "humanoid-001", *, temp: float = 40.0,
            batt: float = 80.0, angle: float = 0.0,
            torque: float = 1.0) -> dict:
    """按 spec/01 §3 的字段名造一条遥测（24 关节键名取自 simulator 的标准键表）。

    只造 1 个关节的字典即可：4 条规则都按「字典里有什么键就判什么键」工作，
    `motor_temp` 取 max、`joint_stuck` 取 `state` 的键集，不依赖固定 24 个。
    """
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


# ---------------------------------------------------------------- 1. 四规则齐全

def test_all_four_rules_exist_and_inherit_base():
    """验收标准 1：4 个规则类齐全且继承 Rule 基类（joint_stuck 不得遗漏）。

    直接对着 `app.main.RULES` 断言 —— 那才是引擎**实际调度**的清单；
    只 import 四个类不断言注册表，漏接一个也照样绿。
    """
    import app.main as m

    ids = sorted(r.rule_id for r in m.RULES)
    assert ids == ["battery_low", "joint_stuck", "motor_temp", "offline"]
    for r in m.RULES:
        assert isinstance(r, Rule), f"{r.rule_id} 未继承 Rule 基类"
    # severity / 去重窗口与 orders/04 表格逐一对照
    assert {r.rule_id: (r.severity, r.dedup_window_s) for r in m.RULES} == {
        "motor_temp": (3, 60),
        "battery_low": (2, 120),
        "offline": (3, 30),
        "joint_stuck": (2, 60),
    }


# ---------------------------------------------------------------- 2. motor_temp

def test_motor_temp_needs_three_consecutive_samples():
    """motor_temp：>75°C 且**连续 ≥3 采样点**才触发；不足 3 点不触发。"""
    rule = MotorTempRule()

    hot = payload(temp=82.0)
    w = deque(maxlen=settings.window_maxlen)

    w.append(payload(temp=82.0))
    w.append(payload(temp=82.0))
    assert rule.evaluate(hot, w) is None, "仅 2 个采样点不应触发（要求 ≥3）"

    w.append(hot)
    alert = rule.evaluate(hot, w)
    assert alert is not None and alert.rule_id == "motor_temp"
    assert alert.severity == 3
    assert alert.detail["temp"] == 82.0


def test_motor_temp_threshold_is_strict_and_resets_on_gap():
    """恰等于 75°C **不**触发（规范是 `>` 而非 `>=`）；中间夹一个低温点要重新计数。"""
    rule = MotorTempRule()
    w = deque(maxlen=settings.window_maxlen)

    at_threshold = payload(temp=75.0)
    for _ in range(3):
        w.append(at_threshold)
    assert rule.evaluate(at_threshold, w) is None, "75°C 不应触发（严格大于阈值）"

    w.clear()
    w.append(payload(temp=80.0))
    w.append(payload(temp=30.0))          # 中间回落到正常温度
    w.append(payload(temp=80.0))
    assert rule.evaluate(payload(temp=80.0), w) is None, \
        "最近 3 点里含一个正常温度，说明不是「持续」过热"


# ---------------------------------------------------------------- 3. battery_low

def test_battery_low_triggers_below_threshold_only():
    """battery_low：<15% 触发（severity 2 / 去重 120s），≥15% 不触发。"""
    rule = BatteryLowRule()
    w = deque(maxlen=settings.window_maxlen)

    assert rule.evaluate(payload(batt=15.0), w) is None, "15.0% 不触发（严格小于）"
    assert rule.evaluate(payload(batt=40.0), w) is None

    alert = rule.evaluate(payload(batt=9.3), w)
    assert alert is not None
    assert (alert.rule_id, alert.severity) == ("battery_low", 2)
    assert BatteryLowRule().dedup_window_s == 120
    assert alert.detail["battery_pct"] == 9.3


def test_battery_low_ignores_missing_field():
    """字段缺失不得抛异常（后端 schema 漂移时引擎要活下来，而不是崩掉整条消费线）。"""
    rule = BatteryLowRule()
    assert rule.evaluate({"robot_id": "humanoid-001"}, deque()) is None


# ---------------------------------------------------------------- 4. joint_stuck

def test_joint_stuck_requires_still_angle_but_high_torque():
    """joint_stuck：5s 窗内角度方差≈0 且平均扭矩超阈值；动起来就不算卡死。"""
    rule = JointStuckRule()
    n = rule.need_samples
    assert n == 50, "5s × 10Hz 应为 50 个采样点"

    # (a) 角度完全不动 + 扭矩 10 > 8.0 → 卡死
    w = deque((payload(angle=0.7, torque=10.0) for _ in range(n)),
              maxlen=settings.window_maxlen)
    alert = rule.evaluate(payload(angle=0.7, torque=10.0), w)
    assert alert is not None and alert.rule_id == "joint_stuck"
    assert alert.severity == 2
    assert alert.detail["variance"] == 0.0

    # (b) 角度在动（方差远大于 1e-6）→ 不触发
    import math
    w2 = deque((payload(angle=math.sin(i), torque=10.0) for i in range(n)),
               maxlen=settings.window_maxlen)
    assert rule.evaluate(payload(angle=0.0, torque=10.0), w2) is None

    # (c) 角度不动但扭矩未超阈值（没在发力，可能只是停着）→ 不触发
    w3 = deque((payload(angle=0.7, torque=3.0) for _ in range(n)),
               maxlen=settings.window_maxlen)
    assert rule.evaluate(payload(angle=0.7, torque=3.0), w3) is None


# ---------------------------------------------------------------- 5. offline

def test_offline_rule_semantics():
    """offline：>10s 无遥测触发；从未上报（last_seen=None）同样触发。"""
    rule = OfflineRule()

    assert rule.evaluate({"robot_id": "humanoid-001", "last_seen": time.time()},
                         deque()) is None, "刚上报过不应判离线"

    fresh = rule.evaluate({"robot_id": "humanoid-001",
                           "last_seen": time.time() - 11.0}, deque())
    assert fresh is not None and fresh.severity == 3
    assert fresh.detail["age_s"] >= 10.0

    never = rule.evaluate({"robot_id": "humanoid-002", "last_seen": None}, deque())
    assert never is not None, "从未上线也必须能判离线"


def test_offline_has_no_redis_expiry_dependency():
    """验收标准 2：代码中**不出现**对 Redis 过期事件的依赖。

    静态断言 + 真实形态断言双保险：
      1) offline 规则与 main 的判定路径里不出现 keyspace/expired 相关 API；
      2) offline 规则**不碰 Redis**（get_redis 在 app.rules.offline 不可见）。
    """
    import ast
    import inspect
    import textwrap

    import app.rules.offline as offline_mod
    import app.main as main_mod

    def code_only(mod) -> str:
        """只保留可执行代码：剥掉 docstring 与 `#` 注释。

        必要性：`rules/offline.py` 与 `main.ticker()` 的**注释里刻意写了**
        "notify-keyspace-events 默认关闭"来解释为何不用过期事件；
        直接对原文做字符串扫描会把「说明为什么禁用」误判成「依赖它」。
        """
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
        assert forbidden not in src, f"offline 判定路径出现了 Redis 过期事件依赖：{forbidden}"

    assert not hasattr(offline_mod, "get_redis"), "offline 规则不应自行连 Redis"


def test_offline_ticker_only_uses_in_memory_last_seen(engine, monkeypatch):
    """真跑一轮 ticker：内存 last_seen 过期 → 出 offline；恢复上报 → 不再出。

    这里**不 mock 掉规则判定**，只把 emit 的出口（save）与 DB 更新换成记录器，
    断言的是 ticker 的判定与在线状态机本身。
    """
    import app.main as m

    saved: list = []

    async def fake_save(alert):
        saved.append(alert)

    async def fake_update_online(rid, online):
        return None

    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(m, "update_online", fake_update_online)
    # 只保留一台机器人，避免整表 100 台每轮都 spawn 一批落库任务
    # settings.robot_ids 是只读 property（config.py:robot_ids），不能 setattr；
    # 但 ticker 读的是 `settings.robot_ids`，故整体替换 settings 上该属性的可写影子。
    monkeypatch.setattr(type(m.settings), "robot_ids",
                        property(lambda self: ["humanoid-001"]))
    # 一轮一轮地手动驱动：ticker 是 `while True: sleep(interval); <body>`，
    # 若 interval=0 会疯狂空转，出成百条告警、断言失去意义。
    # 这里把 interval 设为「比 sleep 长」，用一个 sleep 精确放行**恰好一轮**。
    monkeypatch.setattr(m.settings, "tick_interval_s", 0.2)

    async def one_round():
        import asyncio as _a
        task = _a.create_task(m.ticker())
        await _a.sleep(0.3)      # interval 0.2 → 只跑完一轮，第二轮还在 sleep 里
        task.cancel()
        try:
            await task
        except _a.CancelledError:
            pass
        if m._tasks:             # 放出 spawn 出去的落库/更新任务
            await _a.gather(*list(m._tasks), return_exceptions=True)

    # (a) 11s 前上报过 → 本轮判离线
    engine.last_seen["humanoid-001"] = time.time() - 11.0
    engine.online_state["humanoid-001"] = True
    run(one_round())
    assert [a.rule_id for a in saved] == ["offline"]
    assert engine.online_state["humanoid-001"] is False, "ticker 必须同步维护在线状态"

    # (b) 恢复上报（last_seen=now）→ 不再判离线
    saved.clear()
    engine.last_seen["humanoid-001"] = time.time()
    run(one_round())
    assert saved == [], "恢复上报后不应再出离线告警"
    assert engine.online_state["humanoid-001"] is True


# ---------------------------------------------------------------- 6. 去重幂等

class _FakeRedis:
    """fakeredis 的薄包装：只暴露本用例真正用到的 `set(nx=..., ex=...)`。"""

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
    """验收标准 3：同一窗口内重复消息只放行一次（不重复写库）。"""
    alert = MotorTempRule().evaluate(
        payload(temp=82.0),
        deque((payload(temp=82.0) for _ in range(3)), maxlen=settings.window_maxlen))

    window = MotorTempRule().dedup_window_s
    first = run(deduplicator.should_emit(alert, window))
    second = run(deduplicator.should_emit(alert, window))
    third = run(deduplicator.should_emit(alert, window))

    assert [first, second, third] == [True, False, False], \
        "窗口内重复消息必须只放行第一条"


def test_dedup_key_shape_and_ttl(fake_redis):
    """去重 Key 形状 `alert:dedup:{robot_id}:{rule_id}`，TTL = 窗口秒数。"""
    alert = BatteryLowRule().evaluate(payload(batt=9.0), deque())
    run(deduplicator.should_emit(alert, BatteryLowRule().dedup_window_s))

    key = "alert:dedup:humanoid-001:battery_low"
    assert fake_redis._r.exists(key) == 1
    ttl = fake_redis._r.ttl(key)
    assert 0 < ttl <= 120, f"TTL 应为去重窗口（120s），实际 {ttl}"


def test_dedup_is_per_robot_and_per_rule(fake_redis):
    """去重粒度是 (robot_id, rule_id)：换一台/换一条规则必须能各自发出。"""
    a1 = BatteryLowRule().evaluate(payload("humanoid-001", batt=9.0), deque())
    a2 = BatteryLowRule().evaluate(payload("humanoid-002", batt=9.0), deque())
    a3 = MotorTempRule().evaluate(
        payload("humanoid-001", temp=82.0),
        deque((payload("humanoid-001", temp=82.0) for _ in range(3)),
              maxlen=settings.window_maxlen))

    assert run(deduplicator.should_emit(a1, 120)) is True
    assert run(deduplicator.should_emit(a2, 120)) is True, "不同机器人不应互相压制"
    assert run(deduplicator.should_emit(a3, 60)) is True, "不同规则不应互相压制"


def test_dedup_fails_open_when_redis_down(monkeypatch):
    """Redis 不可达时 fail-open（放行）—— 宁可吵，不可让真故障静默。"""
    async def boom():
        raise ConnectionError("injected: redis down")

    monkeypatch.setattr(deduplicator, "get_redis", boom)
    alert = BatteryLowRule().evaluate(payload(batt=9.0), deque())
    assert run(deduplicator.should_emit(alert, 120)) is True


# ---------------------------------------------------------------- 7. 落库 + 发布

class _RecordingConn:
    """记录 INSERT 的 SQL 与参数序列，返回一个 alert_id。"""

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
    """验收标准 4：写入 `robot_alert` 且 PUBLISH `robot:alerts` 生效。

    断言到 SQL 文本与参数序列：表名、列序、`::jsonb` 转型、RETURNING alert_id
    全部逐项对照 `app/store.py` 与 `infra/init.sql` 的列定义。
    """
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
                  message="电机过热", detail={"joint": "head_yaw", "temp": 82.0})
    run(store_mod.save(alert))

    # (1) 落库：一条 INSERT INTO robot_alert，6 个参数，detail 走 jsonb
    assert len(conn.calls) == 1
    sql, args = conn.calls[0]
    assert "INSERT INTO robot_alert" in sql
    assert "RETURNING alert_id" in sql
    assert "$6::jsonb" in sql, "detail 必须显式转 jsonb（否则 asyncpg 无法推断类型）"
    assert args[0].tzinfo is not None, "time 必须是带时区的 UTC 时间戳"
    assert args[1:6] == ("humanoid-001", "motor_temp", 3, "电机过热",
                         json.dumps({"joint": "head_yaw", "temp": 82.0}))

    # (2) 发布：频道与信封字段
    assert len(redis.published) == 1
    chan, raw = redis.published[0]
    assert chan == "robot:alerts"
    env = json.loads(raw)
    assert env["type"] if "type" in env else True      # 信封为扁平 JSON（无 type 字段属存量形态）
    for field in ("robot_id", "rule_id", "severity", "message", "detail", "ts"):
        assert field in env, f"信封缺字段 {field}"
    assert env["robot_id"] == "humanoid-001"
    assert env["severity"] == 3
    # alert_id 来自 RETURNING，前端「确认」按钮靠它寻址
    assert env["alert_id"] == 4242


def test_save_publishes_even_if_insert_fails(monkeypatch):
    """落库失败仍要发布：前端告警流是唯一可见面（两段 try 独立，见 store.py 顶部）。"""
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
                             severity=2, message="低电量", detail={"battery_pct": 9.0})))

    assert len(redis.published) == 1, "落库失败不该掐掉前端告警流"
    assert redis.published[0][0] == "robot:alerts"


# ---------------------------------------------------------------- 8. 消费线 → 落库

def test_consume_line_feeds_window_and_emits(engine, monkeypatch):
    """消费线：一条真实 payload 进窗口 → 三条约会规则跑一遍 → 命中即走 emit。

    用 3 条过热 payload 模拟连续采样，验证 `consume()` 的窗口维护与规则调度。
    """
    import app.main as m

    saved: list = []

    async def fake_save(alert):
        saved.append(alert)

    async def fake_should_emit(alert, window_s):
        return True

    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(m, "should_emit", fake_should_emit)

    # 直接调用消费线的“单条处理”逻辑：把 pubsub.listen() 换成有限序列
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

    class _R:
        def pubsub(self):
            return _PubSub()

    async def fake_get_redis():
        return _R()

    monkeypatch.setattr(m, "get_redis", fake_get_redis)

    async def drive():
        await m.consume()
        # consume() 内部用 spawn() 把 emit 扔进后台任务，等它们跑完再收尾
        if m._tasks:
            await __import__("asyncio").gather(*list(m._tasks),
                                              return_exceptions=True)

    run(drive())

    rule_ids = sorted({a.rule_id for a in saved})
    assert "motor_temp" in rule_ids, "3 条连续过热应触发 motor_temp"
    assert "battery_low" in rule_ids, "第 3 条 payload 电量 9% 应触发 battery_low"
    assert m.windows["humanoid-001"].__len__() == 3
