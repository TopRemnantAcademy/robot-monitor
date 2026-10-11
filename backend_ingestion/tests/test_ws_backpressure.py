"""WS 出站背压 + 心跳的契约测试（ISSUE #73）。

锁定 ISSUE #73 的三条验收标准与三条「约束/禁止项」，**锁定语义而非实现**：
改实现只要行为不变就不该红。

  验收 ①  发送前检查背压状态（队列深度），超出阈值时丢弃最旧帧 / 限流，
          而非无界堆积；
  验收 ②  ping/pong 保活，异常/超时连接主动 close，并正确走 finally
          回收 `_ws_active`；
  验收 ③  慢客户端不拖垮后端内存；断网后 `_ws_active` 在心跳超时内归位。

  约束 ①  不破坏鉴权顺序（accept → 1008 → 不 send 业务帧）；
  约束 ②  不去掉 `chan_alerts` 转发；
  约束 ③  不为背压丢弃 **alert** 消息。

跑法（必须单独跑，理由见 pytest.ini 与 conftest.py 头注释）：
    pytest backend_ingestion/tests/test_ws_backpressure.py -v

为什么不用真起 uvicorn + 真连 WS：那是 CI/Docker 的活；本文件用
`TestClient` 的 in-process WS（覆盖握手/鉴权/首帧/断开四段语义）与直接
驱动 `WsOutbound`。端到端真连的取证见 PR 描述。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import metrics, ws_backpressure
from app.config import settings
from app.main import app
from app.ws_backpressure import ALERT, TELEMETRY, WsOutbound


# ------------------------------------------------------------------ 基础设施

class BlockingWS:
    """永不消费的客户端替身：第一次 send 就永久阻塞。

    用它模拟"卡死的浏览器标签" —— 这是 ISSUE #73 描述的故障形态。
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
    """正常消费的客户端替身：send 立即可用。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, envelope: dict) -> None:
        self.sent.append(envelope)


async def _settle() -> None:
    """让事件循环把已排队的工作跑完（不给固定时长，减少 flaky）。"""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
def client():
    # 不触发 lifespan：本文件只测路由行为与出站通道，
    # 真起 MQTT/Redis 需要外部依赖，属另一个测试层。
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------- 验收① 背压：内存有界

class TestOutboundIsBounded:
    """ISSUE #73 验收①：队列深度超阈值时丢最旧帧，**绝不无界堆积**。"""

    def test_slow_client_queue_stays_bounded(self):
        """1000 条灌进去，慢客户端队列必须顶在 maxsize，而不是涨到 1000。"""

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=10, alert_slack=2)
            ob.start()
            for i in range(1000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        # 内存有界 = 队列长度是**常量**（物理容量），与灌入条数无关。
        # 物理容量 = 遥测软上限 + 告警预留，两者都在 config 里写死。
        assert ob.qsize <= ob.capacity, f"队列越界：{ob.qsize} > {ob.capacity}"
        assert ob.telemetry_dropped > 0, "慢客户端应触发丢帧"

    def test_drops_oldest_not_newest(self):
        """丢的必须是**最旧**帧：遥测是"最新值优先"的时序数据。"""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=3, alert_slack=0)
            for i in range(10):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return ob, ob.drain_nowait()

        _ob, kept = asyncio.run(drive())
        nums = [e["n"] for e in kept]
        # 队里应留下**最后**几条（最旧的被丢掉），而不是最旧的几条。
        assert nums == sorted(nums), f"顺序被破坏：{nums}"
        assert nums[-1] == 9, f"最新帧必须在：{nums}"
        assert nums[0] > 0, f"最旧的帧应已被丢弃：{nums}"

    def test_misconfigured_maxsize_still_bounds(self):
        """`maxsize` 配成 0/负数也**不得**退化成无界（asyncio.Queue 的 0 = 无限）。

        这是唯一一处"防御性"代码，理由具体：阈值来自 .env，写错一个 0 就等于
        把本单修好的问题原样放回去 —— 而"配置写错"没有任何运行时信号。
        反向验证：把 `max(1, maxsize)` 改回 `maxsize`，这条会红（实测 qsize=999）。
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=0, alert_slack=2)
            ob.start()
            for i in range(1000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        assert ob.maxsize >= 1, "maxsize 退化成了无界（0 在 asyncio 里=无限）"
        assert ob.qsize <= ob.capacity, f"无界堆积：qsize={ob.qsize}"

    def test_enqueue_never_blocks(self):
        """`enqueue` 必须同步返回：它一旦 await，转发循环就被拖住了。"""

        async def drive() -> float:
            ob = WsOutbound(BlockingWS(), maxsize=2, alert_slack=0)
            ob.start()
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for i in range(5000):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return loop.time() - t0

        elapsed = asyncio.run(drive())
        # 5000 次入队若有一次 await 到慢客户端，这里就是 3600s 级。
        assert elapsed < 1.0, f"enqueue 疑似阻塞：{elapsed:.3f}s"


# ------------------------------------------------- 约束③ 告警不可丢（核心）

class TestAlertIsNeverDropped:
    """ISSUE #73 约束③：告警优先级高于遥测节流，**最多合并/延迟，不可丢**。"""

    def test_alert_survives_telemetry_overflow(self):
        """遥测把队列撑满后，告警仍然进得去且不被丢掉。"""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=4)
            for i in range(200):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            for i in range(3):
                ob.enqueue(ALERT, {"type": "alert", "rule_id": "motor_temp", "n": i})
            return ob, ob.drain_nowait()

        _ob, kept = asyncio.run(drive())
        alerts = [e for e in kept if e["type"] == "alert"]
        # 预留的 alert_slack 空间正是为这一幕存在的：遥测把软上限占满后，
        # 告警仍能进来，一条不丢。
        assert len(alerts) == 3, f"告警被丢弃：只留下 {len(alerts)}/3"

    def test_alert_overflow_is_capped_not_unbounded(self):
        """即便告警洪流，队列也不能无界增长 —— 容量封在 maxsize + slack。"""

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=3)
            for i in range(10):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            for i in range(500):
                ob.enqueue(ALERT, {"type": "alert", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())
        assert ob.qsize <= ob.capacity, f"告警路径把队列撑爆：{ob.qsize}"
        # 到顶之后记为 deferred（可观测），而不是静默消失或抛异常。
        assert ob.alerts_deferred > 0, "溢出应被记账"

    def test_alert_flood_does_not_raise(self):
        """告警洪流不得抛异常 —— 抛出去会炸掉整个转发循环。

        回归护栏：本文件第一版草稿在"队列满 + 告警"路径上既抛 QueueFull
        （漏 guard），改成重试后又是 100% CPU 活锁。两种都得被这条挡住。
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=4, alert_slack=2)
            ob.start()
            for i in range(3000):
                ob.enqueue(ALERT, {"type": "alert", "n": i})
            await _settle()
            return ob

        ob = asyncio.run(drive())  # 抛出即测试失败
        assert ob.qsize <= ob.capacity

    def test_telemetry_cannot_eat_the_alert_reserve(self):
        """遥测**不得**占用留给告警的那截容量。

        回归护栏：本文件上一版按**物理容量**判断遥测是否该丢，于是遥测先把
        预留区占满，随后的告警只能顺延 —— `maxsize=5 / slack=4` 时实测
        遥测占 9 格、3 条告警全被 defer，"告警不可丢"名存实亡。
        正确语义是：遥测只准用到 `maxsize`（软上限），预留区只归告警。
        """

        async def drive() -> WsOutbound:
            ob = WsOutbound(BlockingWS(), maxsize=5, alert_slack=4)
            for i in range(200):
                ob.enqueue(TELEMETRY, {"type": "telemetry", "n": i})
            return ob

        ob = asyncio.run(drive())
        assert ob.qsize <= ob.maxsize, (
            f"遥测越界占用了告警预留区：qsize={ob.qsize} > 软上限={ob.maxsize}"
        )
        assert ob.capacity > ob.maxsize, "预留区应当存在（slack > 0）"

    def test_alert_accounting_is_conserved_in_mixed_stream(self):
        """混流下告警必须**守恒**：排队 + 发送中 + 顺延 == 发出的总数。

        回归护栏：本文件上一版用"换队腾位"实现告警优先级，把 `self._queue`
        换成新对象时与正在消费的发送协程竞争，静默丢了告警。
        只数队列里的告警看不出问题（丢的那条不在任何地方），
        必须把"发送中"（已被 sender 取出、正卡在 send_json 上）也算进来才守恒。
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
            f"告警不守恒：排队 {queued} + 发送中 {inflight} + 顺延 {ob.alerts_deferred}"
            f" = {total} != {alerts}（有告警被静默丢弃）"
        )

    def test_alert_kind_uses_dedicated_path(self):
        """`alert` 与 `telemetry` 走的必须是不同分支（分级处置的存在性问题）。"""

        async def drive() -> tuple[WsOutbound, list[dict]]:
            ob = WsOutbound(BlockingWS(), maxsize=2, alert_slack=2)
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 0})
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 1})
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 2})  # 顶满软上限 → 丢最旧
            ob.enqueue(ALERT, {"type": "alert", "n": 0})          # 走告警预留区
            return ob, ob.drain_nowait()

        ob, kept = asyncio.run(drive())
        # 遥测被丢（telemetry_dropped > 0），告警一个没丢（alerts_deferred == 0）。
        assert ob.telemetry_dropped > 0, "遥测应被丢弃"
        assert ob.alerts_deferred == 0, "有预留余量时告警不应顺延"
        assert [e for e in kept if e["type"] == "alert"], "告警必须留下"


# ------------------------------------------------ 验收② 心跳与回收（_ws_active）

#: 回收等待上限。服务端的 finally 相对客户端 ctx 退出是**异步**的
#: （客户端关掉本地 socket 与服务端跑完 teardown 之间隔着一次事件循环调度），
#: 实测稳定在 10ms 量级；给 2s 是为了不因 CI 负载抖动而 flaky。
RECYCLE_WAIT_S = 2.0


def _wait_recycled(expected: int, timeout: float = RECYCLE_WAIT_S) -> bool:
    """轮询等待 `_ws_active` 降到 `expected`。返回是否等到。"""
    import time

    import app.main as main

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if main._ws_active == expected:
            return True
        time.sleep(0.01)
    return main._ws_active == expected


class TestConnectionAccounting:
    """ISSUE #73 验收②：连接异常/超时后必须走 finally 回收 `_ws_active`。"""

    def test_disconnect_recycles_counter(self, client):
        """客户端断开后计数必须归零（改前挂死 —— 本单的核心修复）。

        注意断言的是「**最终**回收」而不是「同步回收」：客户端关闭本地 socket
        与服务端跑完 finally 之间必然有一次调度延迟。把这一步写成同步断言，
        测的就不是产品行为而是调度时序，CI 上会随机飘红。
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        assert token, "探针 token 自签失败，测试前提不成立"
        before = main._ws_active

        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            assert main._ws_active == before + 1, "连上后计数未 +1"

        assert _wait_recycled(before), (
            f"断开后未回收：{main._ws_active}（应回到 {before}）—— "
            "这正是 ISSUE #73 要修的「连接只增不减」"
        )

    def test_repeated_connections_do_not_leak(self, client):
        """连续多轮连接/断开不得让计数漂移（防"漏一次 finally"）。

        串行 6 轮、每轮都等回收 —— 若 finally 漏一次，计数会单调上涨，
        等回收必然超时，因此这条能抓住"偶发漏回收"。
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        before = main._ws_active
        for i in range(6):
            with client.websocket_connect(f"/ws/robots?token={token}") as ws:
                ws.receive_json()
            assert _wait_recycled(before), (
                f"第 {i + 1} 轮后计数未回到 {before}：{main._ws_active}"
            )

    def test_counter_returns_to_baseline_after_many_rounds(self, client):
        """多轮快速重连（模拟前端退避重连风暴）后计数必须回到基线。

        这条与 `test_repeated_connections_do_not_leak` 的区别是**不等回收就重连**：
        它覆盖"上一条还没 teardown 完就来了下一条"的重叠窗口 —— 若 finally 与
        +1 之间存在竞态（例如计数减错对象），重叠时会表现为计数偏移而单条不复现。
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        before = main._ws_active
        for _ in range(10):
            with client.websocket_connect(f"/ws/robots?token={token}") as ws:
                ws.receive_json()
        assert _wait_recycled(before), f"重连风暴后计数未归位：{main._ws_active}"

    def test_rejected_handshake_does_not_touch_counter(self, client):
        """被 1008 拒掉的握手**不得**计入连接数（订单 16 T1b 的语义）。

        先说清基线（等计数归位），再连打两次坏握手 —— 计数必须纹丝不动。
        改前若把 +1 放在鉴权之前，"1008 风暴"（前端带过期 token 反复重连）
        会让连接数虚高，掩盖"大屏到底开着几台"这个真实信号。
        """
        import app.main as main

        from app import ws_probe

        token = ws_probe._mint_token()
        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            ws.receive_json()
        assert _wait_recycled(0), f"基线未归位：{main._ws_active}"
        baseline = main._ws_active

        for bad in ("/ws/robots", "/ws/robots?token=bogus"):
            try:
                with client.websocket_connect(bad) as ws:
                    ws.receive_json()
            except Exception:
                pass
            assert main._ws_active == baseline, (
                f"{bad} 污染了连接计数：{main._ws_active} != {baseline}"
            )

    def test_aclose_is_idempotent(self):
        """`aclose` 可重复调用：finally 与 watchdog 可能都来收一次。"""

        async def drive() -> WsOutbound:
            ob = WsOutbound(DrainingWS(), maxsize=4)
            ob.start()
            ob.enqueue(TELEMETRY, {"type": "telemetry", "n": 0})
            await _settle()
            await ob.aclose()
            await ob.aclose()  # 不得抛
            return ob

        ob = asyncio.run(drive())
        assert ob._task is None


# ------------------------------------------------------------ 约束① 鉴权顺序

class TestAuthOrderPreserved:
    """ISSUE #73 约束①：accept → 1008 → 不 send 业务帧，顺序不得破坏。"""

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
        """合法连接的首帧必须是 snapshot（背压改造不得改变首帧语义）。"""
        from app import ws_probe

        token = ws_probe._mint_token()
        with client.websocket_connect(f"/ws/robots?token={token}") as ws:
            first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert "robots" in first["data"]


# ------------------------------------------------------- 约束② 告警转发仍在

class TestAlertChannelStillForwarded:
    """ISSUE #73 约束②：不得去掉 `chan_alerts` 转发（前端告警流唯一数据源）。"""

    def test_pump_routes_alerts_channel(self):
        """`_ws_pump_pubsub` 必须把 `robot:alerts` 映射成 alert 信封。

        直接喂假 pubsub，断言入队结果是 `alert` 类型 —— 这比"读代码看有没有
        那个 if"可靠：有人把 `chan_alerts` 分支删掉时这条会红。
        """
        import json

        import app.main as main

        alerts_channel = settings.chan_alerts
        stream_channel = settings.chan_stream_all

        class FakePubSub:
            async def listen(self):
                yield {
                    "type": "message",
                    "channel": stream_channel,
                    "data": json.dumps({"robot_id": "humanoid-001"}),
                }
                yield {
                    "type": "message",
                    "channel": alerts_channel,
                    "data": json.dumps({"robot_id": "humanoid-001", "rule_id": "motor_temp"}),
                }

        async def drive() -> WsOutbound:
            ob = WsOutbound(DrainingWS(), maxsize=10)
            await main._ws_pump_pubsub(ob, FakePubSub())
            return ob

        ob = asyncio.run(drive())
        kinds = [e["type"] for e in ob.drain_nowait()]
        assert "alert" in kinds, f"chan_alerts 未被转发：{kinds}"
        assert "telemetry" in kinds, f"chan_stream_all 未被转发：{kinds}"


# -------------------------------------------------------------- 指标与配置

class TestObservabilityAndConfig:
    def test_backpressure_metrics_exist(self):
        text = metrics.render()[0].decode()
        assert "robot_backend_ws_outbound_queue_depth" in text
        assert "robot_backend_ws_telemetry_frames_dropped_total" in text
        assert "robot_backend_ws_alerts_deferred_total" in text
        assert "robot_backend_ws_slow_client_evicted_total" in text

    def test_thresholds_come_from_settings(self):
        """阈值必须来自配置（AGENTS.md：禁止在业务代码里硬编码）。"""
        assert settings.ws_outbound_queue_size > 0
        assert settings.ws_heartbeat_interval_s > 0
        assert settings.ws_outbound_close_timeout_s > 0
        assert settings.ws_alert_overflow_slack >= 0

    def test_alert_slack_is_a_real_margin(self):
        """slack 默认值必须 > 0，否则"告警不可丢"会退化成"先到先得"。"""
        assert ws_backpressure.ALERT_OVERFLOW_SLACK > 0
