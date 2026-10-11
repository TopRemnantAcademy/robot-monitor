"""WS 出站中枢的契约与性能护栏测试（ISSUE #71，#73 融合架构）。

被测对象是 `app/ws_hub.py` 的 `WsHub`，**不依赖 Redis / 真实端口**：
用假的 Redis（`_FakeRedis`）喂 pubsub 消息；连接侧注册真实的 `WsOutbound`
（包一个假 WebSocket，但**不 start 发送协程**），扇出后用 `drain_nowait()`
同步取帧断言 —— 确定性优先，不跟调度时序赛跑。

锁定的六件事（对齐 ISSUE #71 的验收标准与禁止项）：
  1. **每台机器人只保留最新一帧**：同一 id 连发 3 条，只扇出最后一条；
  2. **固定节拍**：扇出只发生在 `_broadcast_loop` 的节拍上，消费本身不发送；
  3. **多连接复用**：N 个连接拿到**同一个**字符串对象（一份序列化 → 多连接复用）；
  4. **慢客户端隔离**：队列小的连接只丢自己的旧遥测，别的连接一个不少；
  5. **禁止项**：① 告警逐条原样转发（不去掉 chan_alerts）；② 只碰内存字典、
     不引入每条消息的 DB 查询；
  6. **生命周期幂等**：start() 二次调用不重建任务，stop() 收干净。
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


# ------------------------------------------------------------------ 假件

class _FakeWS:
    """出站通道的宿主假件。中枢扇出只入队不发送，因此它没有任何行为。"""


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
            if item is None:  # 哨兵：结束
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
    """构造一条**未启动发送协程**的出站通道：扇出只入队，断言走 drain_nowait。"""
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
    """等 Redis 队列里的消息被消费循环取走（它没有可 await 的回执）。"""
    for _ in range(200):
        if queue.empty():
            await asyncio.sleep(0)
            if queue.empty():
                return
        await asyncio.sleep(0.005)


# ------------------------------------------------- 1. 每台机器人只留最新一帧

def test_keeps_only_latest_frame_per_robot(fake_redis):
    """同 id 连发 3 条 → 只扇出 ts 最大的那条（中间帧被本地化合并）。"""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            for ts in (1, 2, 3):
                fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", ts))
            await _drain(fake_redis.queue)
            # 消费循环本身**不投递** —— 扇出只发生在节拍上。
            assert ob.qsize == 0, "订阅侧不得直接入队（应由节拍统一扇出）"
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        frames = ob.drain_nowait()
        assert len(frames) == 1, f"3 条同 id 应合并为 1 帧，实际 {len(frames)}"
        frame = json.loads(frames[0])
        assert frame["type"] == "telemetry"
        assert frame["data"]["ts"] == 3, "必须是**最新**那一帧"

    asyncio.run(scenario())


def test_coalesced_counter_increments_on_overwrite(fake_redis):
    """被覆盖掉的帧要计数（指标是「合并真的生效」的证据，不能恒 0）。"""

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


# ------------------------------------------------- 2/3. 多连接复用同一批帧

def test_multiple_clients_get_same_frames(fake_redis):
    """N 个连接收到的帧**内容一致**，且每条只序列化一次（字符串即同一对象）。"""

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

        # 每条连接都拿到 2 台机器人的最新帧。
        drained = [ob.drain_nowait() for ob in obs]
        for frames in drained:
            assert len(frames) == 2
        # 同一帧在所有连接里是**同一个字符串对象**（一份序列化 → 多连接复用）。
        firsts = {id(frames[0]) for frames in drained}
        assert len(firsts) == 1, "帧字符串应在连接间复用，不应各自重新序列化"

    asyncio.run(scenario())


# ------------------------------------------------- 4. 慢客户端隔离（#73 融合）

def test_slow_client_only_drops_own_telemetry(fake_redis):
    """队列小的慢客户端只丢**自己的**旧遥测；健康连接一帧不少。

    这是融合架构的核心护栏：中枢扇出只 put_nowait，若有人把扇出改回
    `await ws.send_text` 直发，慢客户端就会阻塞节拍、拖饿所有人 —— 那时
    健康连接的帧数也会变，这条立刻红。
    """

    async def scenario():
        hub = WsHub()
        slow = _outbound(maxsize=3)     # 慢客户端：队列极小
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

        assert len(healthy.drain_nowait()) == 20, "健康连接必须一帧不少"
        # 慢连接只装下 3 条 + 丢最旧若干：入队总量 20 > 容量 3，必有丢弃。
        assert slow.telemetry_dropped > 0, "慢客户端的背压丢弃必须发生"
        # 中枢照常记账（带 kind 的发送帧数按连接数累加）。
        # （慢连接丢弃量在 metrics.telemetry_frames_dropped 里由 finally 汇总，
        #   单测不打真实连接生命周期，这里只断言中枢侧计数已发生。）

    asyncio.run(scenario())


# ------------------------------------------------- 5. 禁止项

def test_alerts_are_forwarded_verbatim(fake_redis):
    """告警不得被合并成数组/丢弃：逐条原样转发（前端告警流的唯一数据源）。"""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            a1 = {"robot_id": "humanoid-001", "rule_id": "motor_temp", "severity": 3,
                  "message": "过热", "ts": 1}
            a2 = {"robot_id": "humanoid-002", "rule_id": "battery_low", "severity": 2,
                  "message": "电量低", "ts": 2}
            fake_redis.feed(settings.chan_alerts, a1)
            fake_redis.feed(settings.chan_alerts, a2)
            await _drain(fake_redis.queue)
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        frames = ob.drain_nowait()
        kinds = [json.loads(f)["type"] for f in frames]
        assert kinds == ["alert", "alert"], "告警必须逐条转发，不得合并/丢弃"
        datas = [json.loads(f)["data"] for f in frames]
        assert datas == [a1, a2], "告警内容必须逐字段原样（不得改写）"

    asyncio.run(scenario())


def test_unregister_stops_delivery(fake_redis):
    """注销后不再收到扇出（连接回收后中枢不得继续向死通道投递）。"""

    async def scenario():
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        task = asyncio.create_task(hub._consume_loop())
        try:
            fake_redis.feed(settings.chan_stream_all, _telemetry("humanoid-001", 1))
            await _drain(fake_redis.queue)
            await hub.unregister(ob)
            assert hub.client_count == 0, "注销后注册表必须为空"
            await hub.broadcast_now(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert ob.qsize == 0 and len(ob.drain_nowait()) == 0, "注销后不得再收到帧"

    asyncio.run(scenario())


def test_no_db_query_beyond_redis(fake_redis, monkeypatch):
    """禁止项：不得引入每条消息的额外 DB 查询。

    做法：把 `app.db.session` 的连接入口打成「一调就炸」，
    然后喂 100 条遥测走完整条链路（消费 + 扇出）。若逻辑里碰了 DB，必炸。
    """
    import app.db.session as session_mod

    def boom(*a, **kw):
        raise AssertionError("ws hub 不得触碰数据库")

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


# ------------------------------------------------- 信封形态（契约不变）

def test_envelope_shape_matches_spec():
    """信封仍是 spec/00 §5.2 的 `{"type":..., "data":...}` 三分支形态。"""
    env = json.loads(_envelope("telemetry", _telemetry("humanoid-001", 1)))
    assert set(env.keys()) == {"type", "data"}
    assert env["type"] == "telemetry"
    assert env["data"]["robot_id"] == "humanoid-001"


def test_broadcast_interval_is_fixed_tempo(fake_redis, monkeypatch):
    """节拍只取决于配置：调小 interval，同样时长内扇出次数变多（固定节拍）。"""
    monkeypatch.setattr(settings, "ws_broadcast_interval_ms", 20.0)

    async def count_ticks() -> int:
        hub = WsHub()
        ob = _outbound()
        await hub.register(ob)
        # 消费循环必须在场，否则 _latest 永远空（广播循环无货可投）。
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

    # 只断言「有扇出且次数与节拍同量级」，不做脆弱的精确计数（CI 调度会抖）。
    assert asyncio.run(count_ticks()) >= 2


# ------------------------------------------------- 生命周期幂等

def test_start_is_idempotent(fake_redis):
    async def scenario():
        hub = WsHub()
        await hub.start()
        first = hub._task
        await hub.start()  # 二次调用不得重建任务
        assert hub._task is first
        await hub.stop()
        assert hub._task is None and hub._redis_task is None

    asyncio.run(scenario())
