"""`assert_metric_naming` 的判据回归（订单 34）。

为什么单独一个文件：这个断言是**反例门禁** —— 它必须能红，也必须不误红。
订单 34 的实战（CI 构建 `cnb-dr0-1k4pbtmr7-002`）是「误红」：`robot_backend_batch_flushes_total`
带 label，prometheus_client 惰性注册，零流量下没有样本行，探针把「无 series」判成「指标不存在」，
把整条 e2e stage 打红。修法是按 label 分两类判据（见 `test_e2e_30k.py` 的
`REQUIRED_BACKEND_METRICS` / `LABELED_BACKEND_METRICS`）。

本文件只喂伪造的 /metrics 文本给断言函数，不起栈、不需要 Docker —— 所以它在 `pytest` 裸跑
（testpaths 含 `tests`）里**必然真跑**，是这条判据唯一能在本机验证的地方。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent / "test_e2e_30k.py"
_spec = importlib.util.spec_from_file_location("e2e_30k_probe", _MOD_PATH)
e2e = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(e2e)


# 零流量栈的 /metrics：无 label 的都在，带 label 的 batch_flushes 尚无样本行。
# 结构与真实 backend 的 render() 输出一致（只保留本用例相关的行）。
_ZERO_TRAFFIC = """\
# HELP robot_backend_queue_depth asyncio.Queue 当前积压条数
# TYPE robot_backend_queue_depth gauge
robot_backend_queue_depth 0.0
# HELP robot_backend_batch_flushes_total BatchWriter 的刷批次数
# TYPE robot_backend_batch_flushes_total counter
# HELP robot_backend_telemetry_written_total 成功写入的遥测行数
# TYPE robot_backend_telemetry_written_total counter
robot_backend_telemetry_written_total 0.0
robot_backend_mqtt_received_total 0.0
robot_backend_ws_connections 0.0
robot_backend_history_latency_seconds_count 0.0
robot_backend_history_latency_seconds_sum 0.0
"""

# 压测之后的 /metrics：batch_flushes 有了 {reason=...} 样本行。
_LOADED = _ZERO_TRAFFIC + 'robot_backend_batch_flushes_total{reason="count"} 80.0\n'


def _serve(monkeypatch, text: str) -> None:
    monkeypatch.setattr(e2e, "http_get", lambda url, timeout=5: text)


def test_zero_traffic_allows_unemitted_labeled_counter(monkeypatch):
    """零流量 + allow_unemitted=True：带 label 指标缺席属预期，不得判红。"""
    _serve(monkeypatch, _ZERO_TRAFFIC)
    e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)


def test_zero_traffic_strict_mode_still_reds(monkeypatch):
    """零流量 + 默认（严）口径：带 label 指标缺席必须判红 —— 豁免不是无条件的。"""
    _serve(monkeypatch, _ZERO_TRAFFIC)
    with pytest.raises(AssertionError, match="robot_backend_batch_flushes_total"):
        e2e.assert_metric_naming("http://x/metrics", "robot_")


def test_loaded_stack_requires_full_set(monkeypatch):
    """有流量：即使是最严口径也必须全量在位。"""
    _serve(monkeypatch, _LOADED)
    e2e.assert_metric_naming("http://x/metrics", "robot_")


def test_legacy_name_leak_reds_even_with_exemption(monkeypatch):
    """反例自检：旧前缀指标一旦残留，即使开了豁免也必须红 —— 门禁不能变成永远绿。"""
    _serve(monkeypatch, _LOADED + "robot_monitor_batch_queue_size 1.0\n")
    with pytest.raises(AssertionError, match="robot_monitor_batch_queue_size"):
        e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)


def test_unlabeled_missing_reds_even_with_exemption(monkeypatch):
    """反例自检：无 label 的规范名缺失，豁免不适用，必须红。"""
    _serve(monkeypatch, _LOADED.replace("robot_backend_queue_depth 0.0\n", ""))
    with pytest.raises(AssertionError, match="robot_backend_queue_depth"):
        e2e.assert_metric_naming("http://x/metrics", "robot_", allow_unemitted=True)
