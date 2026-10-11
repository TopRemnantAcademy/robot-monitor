"""订单 16 可观测性契约测试（T1b / T3 / T4）。

本文件锁定的是 `orders/16-dispatch.md` §2 验收标准里**可在无 PG/Redis/MQTT 环境下
验证**的那几条。锁定"验收标准"而非实现细节：改实现只要语义不变就不该红。

跑法（必须单独跑，理由见 pytest.ini 与 conftest.py 头注释）：
    pytest backend_ingestion/tests/

覆盖：
  T1b  /metrics 免鉴权；ISSUE #72 后**只有** robot_backend_* 一套命名（无 robot_monitor_*，
       双轨重复指标已删）；queue Gauge 随队列真实变化（验收要求"不是常量 0"）
  T1b  禁止按 robot_id 分 label（高基数）—— 用指标名断言而非人工检查
  T3   OTel 未配置时 no-op；_traces_url 归一化；缺包时可降级
  T4   日志是单行 JSON，`json.loads` 可解析且含 ts/level/logger/msg
  T4   异常栈也在单行内（不能被换行撕成多行，否则采集器误判日志边界）
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import metrics, telemetry_tracing
from app.logging_json import JsonFormatter, configure_json_logging
from app.main import app

# ISSUE #72：唯一规范命名。robot_backend_* 被现网 PromQL 与抓取目标引用，
# 必须保留；订单 16 新增的 ws_connections / history_latency_seconds 已改名并入。
REQUIRED_BACKEND_METRICS = (
    "robot_backend_telemetry_written_total",
    "robot_backend_batch_flushes_total",
    "robot_backend_queue_depth",
    "robot_backend_mqtt_received_total",
    "robot_backend_mqtt_dropped_total",
    "robot_backend_ws_connections",
    "robot_backend_history_latency_seconds",
)

# 双轨命名统一（ISSUE #72）后，这些前缀**不得**再出现在 /metrics 里。
# 用前缀整体断言而非逐名断言：防止日后又有人「顺手」新建一个 robot_monitor_*。
FORBIDDEN_MONITOR_PREFIX = "robot_monitor_"


@pytest.fixture
def client():
    """TestClient 不跑 lifespan（不连 MQTT/Redis/PG），路由与依赖解析完全真实。"""
    return TestClient(app, raise_server_exceptions=False)


class TestMetricsEndpoint:
    """/metrics 必须免鉴权（Prometheus pull），且缺任一指标即验收失败。"""

    def test_metrics_is_unauthenticated(self, client):
        """订单 16 禁止项 2：给 /metrics 加鉴权 = 监控静默失效。

        Prometheus 的 scrape 配置里没有 Bearer token，加鉴权后抓取直接 401，
        表现为 `up{job="robot_backend"}=0`，而不是报错 —— 最容易被忽略的失效形态。
        """
        assert client.get("/metrics").status_code == 200

    def test_all_backend_metrics_exposed(self, client):
        """ISSUE #72 验收：唯一命名 robot_backend_* 的 7 个指标全部暴露。"""
        body = client.get("/metrics").text
        missing = [n for n in REQUIRED_BACKEND_METRICS if n not in body]
        assert not missing, f"缺失指标: {missing}"

    def test_legacy_metrics_preserved(self, client):
        """现网 PromQL/抓取目标依赖的 5 个 robot_backend_* 必须仍在（严禁删除）。"""
        body = client.get("/metrics").text
        missing = [n for n in REQUIRED_BACKEND_METRICS[:5] if n not in body]
        assert not missing, f"既有指标被误删: {missing}"

    def test_no_dual_track_monitor_prefix(self, client):
        """ISSUE #72：双轨命名统一后，/metrics 里不得再有任何 robot_monitor_* 指标。

        断言的是**指标名**（行首或 label 名），不是注释 —— 注释里提到历史命名是允许的，
        因为那是迁移记录；只有真正注册进 registry 的时序才算违规。
        """
        body = client.get("/metrics").text
        leaked = [
            line for line in body.splitlines()
            if line.startswith(FORBIDDEN_MONITOR_PREFIX)
        ]
        assert not leaked, f"残留 robot_monitor_* 指标: {leaked}"

    def test_flush_counter_has_reason_label(self, client):
        """按触发原因分 label 是"双重触发是否按设计工作"的唯一证据。

        注意 Counter 的 label 系列是**惰性注册**的：`robot_backend_batch_flushes_total{...}`
        只有在第一次 `labels(reason=...).inc()` 之后才出现（Prometheus 官方行为，
        不是本实现的缺陷）。因此这里必须先**制造一次观测**，再断言 label 存在 ——
        直接抓取空 registry 会拿到 0 行，从而误报"没有 label"。
        """
        from app import metrics

        metrics.observe_flush("count", 7)
        body = client.get("/metrics").text
        assert 'robot_backend_batch_flushes_total{reason="count"}' in body

    def test_observe_flush_rejects_unknown_reason(self, client):
        """脏 reason 归一到 'unknown' 而不是静默丢弃 —— 让拼写错误在面板上可见。"""
        from app import metrics

        metrics.observe_flush("typo-value", 1)
        assert 'robot_backend_batch_flushes_total{reason="unknown"}' in client.get(
            "/metrics"
        ).text

    def test_string_robot_id_never_used_as_label(self, client):
        """订单 16 禁止项 1：禁止按 robot_id 做 label（100 台高基数压垮 TSDB）。

        以 `humanoid-` 作为"是否把 robot_id 写进 label"的探针：
        robot_id 前缀恒为 humanoid-（AGENTS.md §4），出现在 label 位置即违规。
        """
        body = client.get("/metrics").text
        for line in body.splitlines():
            if line.startswith("#") or "{" not in line:
                continue
            label_part = line.split("{", 1)[1].split("}", 1)[0]
            assert "humanoid-" not in label_part, f"robot_id 泄漏进 label: {line}"

    def test_queue_gauge_tracks_real_depth(self, client):
        """验收标准：queue_size 必须有真实波动，不是常量 0。

        直接给 writer.queue 塞元素再抓取 —— 若 Gauge 没在 /metrics 里刷新
        （拉模型下没人替我们 set），这条会拿到 0。
        """
        from app.main import writer

        def read_gauge() -> float:
            for line in client.get("/metrics").text.splitlines():
                if line.startswith("robot_backend_queue_depth "):
                    return float(line.split()[1])
            raise AssertionError("robot_backend_queue_depth 未暴露")

        before = read_gauge()
        for _ in range(5):
            writer.queue.put_nowait(object())  # 内容无关，只验证 Gauge 刷新
        after = read_gauge()
        assert after == before + 5, f"Gauge 未随队列变化: {before} -> {after}"


class TestOtelDegradability:
    """订单 16 §1 T3 + §2：未配置必须 no-op，**绝不能**导致启动失败。"""

    def test_empty_endpoint_is_noop(self):
        assert telemetry_tracing.configure_tracing("") is False

    def test_traces_url_appends_path_once(self):
        assert telemetry_tracing._traces_url("http://c:4318") == "http://c:4318/v1/traces"
        assert (
            telemetry_tracing._traces_url("http://c:4318/v1/traces")
            == "http://c:4318/v1/traces"
        )
        assert telemetry_tracing._traces_url("http://c:4318/") == "http://c:4318/v1/traces"

    def test_import_main_needs_no_otel_packages(self):
        """OTel import 全在函数体内 —— 模块顶层零依赖。

        这是"可降级"的结构性保证：即使环境里没有 opentelemetry-*，
        `import app.main` 也必须成功（本文件本身就在没有 OTel 的 CI 里跑）。
        """
        import app.main  # noqa: F401 —— 能 import 到这一行就是断言


class TestJsonLogging:
    """订单 16 T4：单行 JSON，可被 jq 解析。"""

    def test_record_is_single_line_json(self):
        fmt = JsonFormatter()
        rec = logging.LogRecord(
            "demo", logging.INFO, __file__, 1, "hello %s", ("world",), None
        )
        out = fmt.format(rec)
        assert "\n" not in out, "单行契约被破坏：采集器会误判日志边界"
        payload = json.loads(out)  # 会被 jq 解析的等价断言
        assert payload["level"] == "INFO"
        assert payload["logger"] == "demo"
        assert payload["msg"] == "hello world"
        assert payload["ts"].endswith("Z")

    def test_traceback_stays_single_line(self):
        fmt = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            rec = logging.LogRecord(
                "demo", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        out = fmt.format(rec)
        assert "\n" not in out, "异常栈把日志撕成多行"
        assert "ValueError: boom" in json.loads(out)["exc"]

    def test_extra_robot_id_is_preserved(self):
        """结构化字段（如 robot_id）必须出现在 JSON 顶层，供采集侧索引。"""
        fmt = JsonFormatter()
        rec = logging.LogRecord("demo", logging.INFO, __file__, 1, "x", (), None)
        rec.robot_id = "humanoid-042"
        assert json.loads(fmt.format(rec))["robot_id"] == "humanoid-042"

    def test_configure_is_idempotent(self):
        """重复配置不得叠加 handler（叠加会让同一行日志打印 N 次）。"""
        configure_json_logging()
        n1 = len(logging.getLogger().handlers)
        configure_json_logging()
        n2 = len(logging.getLogger().handlers)
        assert n1 == n2 == 1
