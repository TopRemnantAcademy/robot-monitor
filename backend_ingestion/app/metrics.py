"""Prometheus 指标暴露（后端采集层）。

为什么需要这个文件（此前是「下了没接」的典型）：
  `prometheus/client_python` 在上一轮已 clone 到 `external/`，
  但 `backend_ingestion/` 里**没有任何一处 import** ——
  没有指标导出，配置再多 Prometheus 抓取目标也只会拿到 `up=0`。

  本模块把已经在 `/healthz` 里暴露的运行时计数**同时**导出为 Prometheus 指标，
  于是 `up{job="robot_backend"}` 与 `robot_backend_queue_depth` 等
  在 `infra/observability/prometheus.yml` 里声明的指标**真实存在**。

指标命名对齐 `infra/observability/rules/robot-alerts.yml` 的 PromQL：
  robot_backend_queue_depth          （直方图不需要，Gauge 即可）
  robot_backend_mqtt_dropped_total   （Counter，规则里用 increase(...[1m])）
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

# 独立 registry：避免与进程内其它库的默认注册表互相污染，
# 也让 test 可以拿到干净的实例（默认 REGISTRY 是全局单例，重复导入会抛重复注册）。
registry = CollectorRegistry()

telemetry_written = Counter(
    "robot_backend_telemetry_written_total",
    "成功写入 TimescaleDB 的遥测行数（按批累加，单位：行）",
    registry=registry,
)
batch_flushes = Counter(
    "robot_backend_batch_flushes_total",
    "BatchWriter 的刷批次数（满 500 条或满 1s 双重触发）",
    registry=registry,
)
queue_depth = Gauge(
    "robot_backend_queue_depth",
    "asyncio.Queue 当前积压条数（持续增长=落库跟不上 1000 msg/s）",
    registry=registry,
)
mqtt_received = Counter(
    "robot_backend_mqtt_received_total",
    "MQTT 收到的遥测消息数",
    registry=registry,
)
mqtt_dropped = Counter(
    "robot_backend_mqtt_dropped_total",
    "MQTT 校验失败被丢弃的消息数（>0 说明 payload 契约被破坏）",
    registry=registry,
)


def render() -> tuple[bytes, str]:
    """返回 (body, content_type)，供 FastAPI 的 /metrics 端点直接使用。"""
    return generate_latest(registry), CONTENT_TYPE_LATEST
