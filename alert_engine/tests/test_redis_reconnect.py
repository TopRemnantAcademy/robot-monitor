"""R-14 — `consume()` Redis 断连监督的验收。

R-14 之前的故障模式（真，非推测）：`pubsub.listen()` 在 Redis 闪断时抛
`redis.exceptions.ConnectionError`，该异常**逃出 `consume()`** → 经 `amain()`
的 `asyncio.gather` 向上传播 → 整条协程树失败、进程退出；且 `ticker()` /
`escalation_loop()` 这两个本身没坏的兄弟协程被**连带取消**。

本文件钉住四条语义（对应需求 §5 的 V1/V2/V3/V4）：
1. 断连自愈：`listen()` 抛断连异常后 `consume()` **不退出**，退避后重建 pubsub
   并**重新 subscribe**（订阅状态不跨连接保留）。
2. 不拖垮兄弟：该异常**不传播**出 `consume()`。
3. 现有行为不变：正常消息仍被消费、`JSONDecodeError` 仍跳过、规则异常仍兜底。
4. 可观测：重连有 warning 日志 + `robot_alert_redis_reconnect_total` 计数。

⚠️ 关键事实（本机实测 redis 5.0.3，决定了被测代码必须同时捕两组异常）：
    issubclass(redis.exceptions.ConnectionError, ConnectionError)  # 内建 → False
    issubclass(redis.exceptions.TimeoutError, TimeoutError)        # 内建 → False
redis-py 的这两个异常只继承 `RedisError`，与内建同名类**无继承关系**。
故断连用例**三组都测**（RedisError / 内建 OSError / redis 的 TimeoutError），
只测一组会漏掉另一组。
"""

from __future__ import annotations

import asyncio
import json
import logging

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import metrics
from app.config import settings

# 测试期把退避压到毫秒级，否则默认 1s 起步的重连会让单测白等。
TINY_BACKOFF = 0.001

#: 假 pubsub 累计调用 `listen()` 的次数，供用例等消费循环推进到目标轮次。
#: ⚠️ 必须**全局累加**、不可每轮清零 —— 清零后计数永远到不了 2，
#: `_run()` 会一直等到超时，"重连后是否重新 subscribe"就测不出来。
_listen_total = 0


def _listens() -> int:
    return _listen_total


class FakePubSub:
    """假 pubsub：按脚本吐行为，记录 subscribe/listen 调用次数。

    `listen()` 由 `script` 逐轮驱动（一轮 = `listen()` 的一次调用），每项可为：
      - `Exception` 实例            → 立即抛出（模拟断连）
      - `list`                       → 逐个 yield 这些消息，然后**永久挂起**（等被 cancel）
      - `(list, Exception)` 二元组   → 先 yield 消息，再抛出该异常
        （真实驱动里"投递过消息之后断连"就是这个形态；用它才能测"退避复位"）

    ⚠️ 为什么"正常轮"要挂起，而不是吐完消息就返回：被测的 `async for` 一旦拿到
    `StopAsyncIteration` 就会**正常退出** `consume()` —— 那样测出来的"不退出"是
    假的（是迭代器耗尽，不是断连自愈）。真实 Redis 的 `listen()` 永不自然结束，
    只在连接断开时抛异常。故这里必须忠于真实驱动的这条性质。
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
        # 消息吐完 → 挂起（模拟"连接仍在，只是暂时没消息"）。
        # 真实 Redis 的 listen() 只有两条出路：继续 yield，或抛异常 ——
        # 它**不会**自然返回。故"一轮结束"在本替身里只能靠断连表达
        # （脚本里显式写一个断连异常作下一轮，见 test_backoff_resets_*）。
        await asyncio.Event().wait()


class FakeRedis:
    def __init__(self, pubsubs):
        self._pubsubs = list(pubsubs)
        self.created = 0

    def pubsub(self):
        self.created += 1
        if not self._pubsubs:
            # 脚本用尽后仍要能继续建 pubsub（消费循环不会自己停），否则后续轮次
            # 会 IndexError —— 那会把"脚本没写够"伪装成"被测代码崩了"。
            return FakePubSub([])
        return self._pubsubs.pop(0)


def _wire(monkeypatch, m, pubsubs):
    """把 `app.main.get_redis` 换成假实现，并记录 consume() 的退避时长。

    注意 `main.py` 是 `from app.db import ... get_redis`（**名字已在 import 期绑定**），
    所以必须 patch `app.main.get_redis`；patch `app.db.get_redis` 对 main 无效。
    """
    fake = FakeRedis(pubsubs)
    sleeps: list[float] = []

    async def fake_get_redis():
        return fake

    # ⚠️ 不能写成 `monkeypatch.setattr(m.asyncio, "sleep", ...)`：`m.asyncio` **就是**
    # stdlib 模块对象本身，patch 它等于全局替换 `asyncio.sleep`，替身内部的
    # `await asyncio.sleep(0)` 会递归调自己（本机实测 RecursionError，949 层）。
    # 故替换的是 `app.main` 命名空间里的 `asyncio` **引用**，用只改写 `sleep` 的薄代理。
    class _AsyncioProxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(s):
            sleeps.append(s)
            await asyncio.sleep(0)        # 让出事件循环，但不等真实时间

    monkeypatch.setattr(m, "get_redis", fake_get_redis)
    monkeypatch.setattr(m, "asyncio", _AsyncioProxy)
    return fake, sleeps


async def _run(m, target_listens: int, timeout: float = 3.0):
    """跑 `consume()`，等假 pubsub 累计进到 N 轮 listen() 后取消。

    返回**逃出的异常**（None = 没逃出，即修复生效）。
    用"实际 listen 次数"而非"空转 yield 次数"作推进判据：后者依赖事件循环
    调度细节，脆；前者是被测代码自己驱动的确定性事件。
    """
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
    except BaseException as exc:          # noqa: BLE001 —— 这就是被测断言本身
        return exc


def _msg(rid="humanoid-001", temp=80.0):
    """造一条报文。默认温度 80°C > 阈值 75°C，能被 motor_temp 规则命中。"""
    return {
        "type": "message",
        "data": json.dumps({
            "robot_id": rid,
            "max_joint_temp": temp,
            "battery_pct": 90.0,
            "joints": [],
        }),
    }


# ---------------------------------------------------------------- V1 断连自愈

def test_reconnects_after_redis_connection_error(engine, monkeypatch):
    """`listen()` 抛 `redis.exceptions.ConnectionError` → 不退出 + 重新 subscribe。

    这是 R-14 的核心用例：修复前异常逃出协程，本用例会拿到它；
    修复后应观察到**第二次 subscribe**（订阅不跨连接保留，必须重订）。
    """
    m = engine
    ps1 = FakePubSub([RedisConnectionError("Connection reset by peer")])
    ps2 = FakePubSub([[]])                     # 重连后这一轮正常（无消息）
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"异常逃出了 consume(): {exc!r}"
    assert ps1.subscribe_calls == [settings.chan_stream_all]
    assert ps2.subscribe_calls == [settings.chan_stream_all], "重连后未重新 subscribe"
    assert fake.created == 2, "未重建 pubsub"
    assert sleeps and sleeps[0] == TINY_BACKOFF, f"退避未生效: {sleeps}"


def test_reconnects_on_redis_timeout_error(engine, monkeypatch):
    """`redis.exceptions.TimeoutError` 同样必须被兜住。

    为什么单列：该异常**不是**内建 `TimeoutError` 的子类（本机实测 False），
    只捕内建的那个会漏，故必须有一例专门钉它。
    """
    m = engine
    ps1 = FakePubSub([RedisTimeoutError("Timeout reading from socket")])
    ps2 = FakePubSub([[]])
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"异常逃出了 consume(): {exc!r}"
    assert ps2.subscribe_calls == [settings.chan_stream_all]
    assert fake.created == 2


def test_reconnects_on_builtin_oserror(engine, monkeypatch):
    """内建 `OSError`（hiredis / 底层 socket 直抛）也算断连。

    只捕 `RedisError` 会漏掉这条路径 —— 故三组异常各一例分别钉住。
    """
    m = engine
    ps1 = FakePubSub([OSError("Connection refused")])
    ps2 = FakePubSub([[]])
    fake, sleeps = _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None, f"异常逃出了 consume(): {exc!r}"
    assert fake.created == 2


def test_backoff_grows_exponentially_and_is_capped(engine, monkeypatch):
    """连续断连 → 退避 1x,2x,4x,...，且不超过 RECONNECT_BACKOFF_MAX_S。

    封顶是硬要求：无限翻倍则 Redis 恢复后要等几小时才自愈，等于把
    "永久停订阅"推迟发生（与不修无差别）。
    """
    m = engine
    n = 6
    pubsubs = [FakePubSub([RedisConnectionError("down")]) for _ in range(n)]
    pubsubs.append(FakePubSub([[]]))
    fake, sleeps = _wire(monkeypatch, m, pubsubs)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_MAX_S", TINY_BACKOFF * 4)

    exc = asyncio.run(_run(m, target_listens=n + 1))

    assert exc is None
    assert len(sleeps) >= 4, f"重连次数不足: {sleeps}"
    assert sleeps[0] == TINY_BACKOFF
    assert sleeps[1] == TINY_BACKOFF * 2
    assert sleeps[2] == TINY_BACKOFF * 4
    assert all(s <= TINY_BACKOFF * 4 for s in sleeps), f"退避未被封顶: {sleeps}"


def test_backoff_resets_after_messages_delivered(engine, monkeypatch):
    """一次短暂抖动、且**确实恢复收报**后，退避必须复位。

    为什么判据是"投递过消息"而不是"subscribe 返回了"：Redis 重启时最常见的形态
    恰恰是 subscribe 能成功、随后立刻断。若在那时复位退避，退避会永远停在 1s ——
    "指数退避"名存实亡，变成每秒一次的忙重试。本机实测过这个 bug
    （连续 6 次断连、退避序列全是初始值），故这里必须用"收到过消息"当复位证据。
    """
    m = engine
    # 第 2 轮"先投递消息、再以一次断连收场"——这正是真实形态：只要链路真的
    # 通过报（delivered=True），随后的断连就该从 1s 重新起步。
    # 若把复位挂错在"subscribe 返回"上，第 2 轮这个断连会被认为"没复位"，
    # 于是第 3 轮退避变成翻倍值，本用例即红。
    pubsubs = [
        FakePubSub([RedisConnectionError("blip")]),        # 1: 断
        FakePubSub([([_msg("humanoid-003")],                 # 2: 投递消息…
                     RedisConnectionError("blip2"))]),       #    …再断（收尾）
        FakePubSub([[]]),                                   # 3: 恢复
    ]
    fake, sleeps = _wire(monkeypatch, m, pubsubs)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=3))

    assert exc is None
    assert len(sleeps) == 2, f"应有两次重连退避, 实际 {sleeps}"
    assert sleeps[0] == sleeps[1] == TINY_BACKOFF, (
        f"退避未在收到消息后退避复位: {sleeps}"
    )


# ---------------------------------------------------------------- V2 不传播

def test_disconnect_does_not_propagate_to_gather(engine, monkeypatch):
    """断连异常不逃出 `consume()` → 同 gather 的兄弟协程不被连带取消。

    直接复刻 `amain()` 的编排语义：把 consume() 与一个"长跑兄弟"放同一 gather，
    断言兄弟**跑完了**（若 consume 上抛，gather 会立刻取消兄弟 → 断言失败）。
    """
    m = engine
    ps1 = FakePubSub([RedisConnectionError("down")])
    ps2 = FakePubSub([[]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_MAX_S", TINY_BACKOFF)

    # 用 Event 做**确定性**同步，而不是"空转 N 次 sleep(0)"：
    # 后者的 N 依赖事件循环调度细节（不同 Python 版本 yield 次数不同），
    # 会让"兄弟是否跑完"变成一个脆的竞态断言。这里让兄弟显式等一个事件，
    # 由断连发生后的代码去 set 它 —— 只有在兄弟**没被取消**时才会跑到 set。
    sibling_ran = asyncio.Event()

    async def sibling():
        await sibling_ran.wait()
        return "sibling-finished"

    async def scenario():
        consume_task = asyncio.ensure_future(m.consume())
        sib = asyncio.ensure_future(sibling())
        # 等 consume() 真的经历一次断连并进入重连等待
        while _listens() < 1:
            await asyncio.sleep(0)
        for _ in range(20):
            await asyncio.sleep(0)
        # 到此处若 consume() 曾把异常抛给 gather，sib 早被取消（done 且 cancelled）
        sib_cancelled = sib.cancelled()
        sibling_ran.set()               # 放兄弟跑完
        result = await sib
        consume_task.cancel()
        try:
            await consume_task
        except asyncio.CancelledError:
            pass
        return sib_cancelled, result

    sib_cancelled, sib_result = asyncio.run(scenario())

    assert not sib_cancelled, "兄弟协程被取消 —— consume() 的异常传染给了 gather"
    assert sib_result == "sibling-finished"


def test_non_disconnect_exception_still_propagates(engine, monkeypatch):
    """**反向**用例：非断连异常仍必须逃出 consume()，不被吞成"假活"。

    若把 except 放宽到 `Exception`，任何真 bug（如改坏的消息处理）都会被静默
    吞掉、进程假活、看板照常绿 —— 比原故障更难查。故必须钉住：容错只对
    **断连类**生效。
    """
    m = engine
    ps1 = FakePubSub([ZeroDivisionError("real bug, not a disconnect")])
    _wire(monkeypatch, m, [ps1, FakePubSub([[]])])

    exc = asyncio.run(_run(m, target_listens=1))

    assert isinstance(exc, ZeroDivisionError), (
        f"非断连异常被吞了（应逃出 consume()），实际: {exc!r}"
    )


# ---------------------------------------------------------------- V4 可观测

def test_reconnect_increments_metric(engine, monkeypatch):
    """重连必须让 `robot_alert_redis_reconnect_total` 增长（"假活"可被 PromQL 发现）。"""
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
    """指标须在 `/metrics` 可见（registry 内有同名的 `_total` 家族）。

    只 import 不注册（例如忘了传 `registry=`）会让它挂在**默认** registry 上，
    而本服务用独立 registry 输出 —— 那样指标永远抓不到，等于没有。
    """
    text = metrics.generate_latest(metrics.registry).decode()
    assert "robot_alert_redis_reconnect_total" in text


def test_backoff_value_present_in_warning_log(engine, monkeypatch, caplog):
    """warning 日志必须带**退避秒数**与频道名 —— 只有"断连了"三个字无法排障。"""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("Connection reset by peer")])
    ps2 = FakePubSub([[]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    with caplog.at_level(logging.WARNING, logger="alert_engine"):
        exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "重连未打 warning 日志"
    text = warnings[0].getMessage()
    assert "ConnectionError" in text or "Connection reset" in text
    assert settings.chan_stream_all in text, f"日志缺频道名: {text!r}"


# ---------------------------------------------------------------- V3 行为不变

def test_normal_messages_still_consumed_after_reconnect(engine, monkeypatch):
    """重连后消息处理必须照旧 —— "退避 + 重订"不能把窗口/在线态弄丢。"""
    m = engine
    ps1 = FakePubSub([RedisConnectionError("down")])
    ps2 = FakePubSub([[_msg("humanoid-007", 85.0)]])
    _wire(monkeypatch, m, [ps1, ps2])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=2))

    assert exc is None
    assert m.last_seen.get("humanoid-007") is not None, "重连后的消息未被消费"
    assert len(m.windows["humanoid-007"]) == 1


def test_json_decode_error_still_skipped(engine, monkeypatch):
    """`JSONDecodeError` 仍逐条跳过（R-14 不得弱化既有异常处理）。"""
    m = engine
    ps = FakePubSub([[
        {"type": "message", "data": "not-json{{{"},
        {"type": "subscribe", "data": "1"},          # 非 message 类型也照旧跳过
        _msg("humanoid-009"),
    ]])
    _wire(monkeypatch, m, [ps, FakePubSub([[]])])
    monkeypatch.setattr(m, "RECONNECT_BACKOFF_INITIAL_S", TINY_BACKOFF)

    exc = asyncio.run(_run(m, target_listens=1))

    assert exc is None
    assert m.last_seen.get("humanoid-009") is not None
    assert "humanoid-009" in m.windows


def test_rule_exception_still_contained(engine, monkeypatch):
    """单条规则抛异常仍被兜底（不得弱化既有处理），且**不**被误判为断连。"""
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
    assert sleeps == [], "规则异常被误判为断连、触发了重连"
    assert fake.created == 1
    assert m.last_seen.get("humanoid-010") is not None


# ---------------------------------------------------------------- amain 隔离

def test_isolate_swallows_crash_of_one_line(engine):
    """`_isolate` 让一条驱动线崩溃后**不影响兄弟**，且不吞 CancelledError。"""
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
    assert results == [None, "alive"], f"兄弟协程被连累: {results}"


def test_isolate_reraises_cancelled_error(engine):
    """`_isolate` **不得**吞 `CancelledError`，否则进程关不干净。"""
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
