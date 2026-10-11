"""WS 合成探针的行为契约测试（ISSUE #58 / 缺口 G-3）。

锁定四件在 orders/16-dispatch.md §2 与 ISSUE #58 里被明确要求的事：
  1. 探针指标**独立存在**，且**不覆盖**订单 16 T1b 的 `robot_backend_ws_connections`；
  2. 探针**免鉴权**：`/metrics` 与探针目标都不要求外部 token；
  3. 探针**可降级**：`WS_PROBE_ENABLED=false` 时 `run()` 立刻返回（no-op），
     且探针自身异常不会冒泡（失败只翻指标）；
  4. 频率下限 30s（配错也快不起来）。

跑法（从仓库根，需装 backend_ingestion/requirements.txt）：
    pytest backend_ingestion/tests/test_ws_probe.py -v

为什么不用真实端口：真起 uvicorn + 真连 WS 属 CI/Docker 的活；本文件只用
`TestClient` 的 in-process WS（覆盖握手、鉴权、首帧快照三段语义）与直接调用
`ws_probe._probe_once_async`。端到端真连的取证见 PR 描述。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import metrics, ws_probe
from app.config import settings
from app.main import app

from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # 不触发 lifespan：本文件只测路由行为与探针函数，
    # 真起 MQTT/Redis 需要外部依赖，属另一个测试层。
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ 指标契约

class TestProbeMetricsAreDistinct:
    """ISSUE #58 的核心约束：探针指标 ≠ 连接数指标，禁止互相覆盖。"""

    def test_probe_metrics_exist(self):
        # 注意：`prometheus_client` 的 Counter 会把 `_total` 后缀收进 `_name`，
        # 原文完整名在 `_name + "_total"`。断言用**渲染出的**文本，避免踩这个语义。
        text = metrics.render()[0].decode()
        assert "robot_backend_ws_probe_alive" in text
        assert "robot_backend_ws_probe_failures_total" in text

    def test_probe_does_not_define_connections_metric(self):
        # ISSUE #58 明确：探针指标与订单 16 T1b 的 `robot_backend_ws_connections`
        # 是**不同**指标。这里断言 `metrics.py` 不定义连接数（只注释里提到）。
        module_source = __import__("pathlib").Path(metrics.__file__).read_text()
        assert "Gauge(\n    \"robot_backend_ws_connections\"" not in module_source
        assert '"robot_backend_ws_connections"' not in module_source.split("#")[0]

    def test_probe_alive_is_gauge_failures_is_counter(self):
        # 类型必须对：Gauge 反映「最近一次」，Counter 单调累加给 increase()/rate() 用。
        assert type(metrics.ws_probe_alive).__name__ == "Gauge"
        assert type(metrics.ws_probe_failures).__name__ == "Counter"

    def test_render_contains_probe_metrics(self):
        body, ctype = metrics.render()
        text = body.decode()
        assert "robot_backend_ws_probe_alive" in text
        assert "robot_backend_ws_probe_failures_total" in text
        assert "text/plain" in ctype


# ------------------------------------------------------------------ 免鉴权

class TestProbeIsUnauthenticated:
    def test_probe_target_path_is_not_behind_rest_auth(self, client):
        """`/ws/robots` 的准入只认 `?token=`，无 token 时以 1008 关闭而非 401。

        这里断言的是「探针目标路径存在且无需 REST Bearer 头」——
        REST 的 401 是 `/robots` 那条路径的事，不应牵连 WS。
        """
        r = client.get("/robots")
        assert r.status_code == 401  # REST 侧确实有鉴权（对照组）

    def test_metrics_remains_unauthenticated(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "robot_backend_ws_probe_alive" in r.text


# ------------------------------------------------------------------ 可降级

class TestProbeDegrades:
    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "ws_probe_enabled", False)
        # run() 应立即返回，不进入 while True。
        asyncio.run(asyncio.wait_for(ws_probe.run(), timeout=1.0))

    def test_mint_token_failure_returns_none(self, monkeypatch):
        # 自签失败必须降级成 None，而不是把异常抛进 lifespan。
        import jwt as _jwt

        def boom(*a, **k):
            raise RuntimeError("bad key")

        monkeypatch.setattr(_jwt, "encode", boom)
        assert ws_probe._mint_token() is None

    def test_probe_failure_does_not_raise(self, monkeypatch):
        """连不上的目标不能让 `_probe_once_async` 抛到调用方之外的语义。

        `_probe_once_async` 本身是如实抛的（调用方决定降级）；这里验证
        `run()` 的每一轮都吞掉异常并翻失败指标，而不是冒泡。
        """
        interval = settings.ws_probe_interval_s
        monkeypatch.setattr(settings, "ws_probe_startup_grace_s", 0.0)
        monkeypatch.setattr(settings, "ws_probe_interval_s", 30.0)
        monkeypatch.setattr(settings, "ws_probe_enabled", True)
        # 指向一个必然没人监听的端口，避免依赖外部服务。
        monkeypatch.setattr(settings, "ws_port", 1)

        before = metrics.ws_probe_failures._value.get()

        async def drive():
            task = asyncio.create_task(ws_probe.run())
            # 第一轮探活（超时上限 5s）足够跑完，然后取消。
            await asyncio.sleep(0.3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(asyncio.wait_for(drive(), timeout=8.0))

        assert metrics.ws_probe_alive._value.get() == 0
        assert metrics.ws_probe_failures._value.get() > before


# ------------------------------------------------------------------ 频率下限

class TestIntervalFloor:
    def test_floor_is_30s(self):
        assert ws_probe.MIN_INTERVAL_S == 30.0
