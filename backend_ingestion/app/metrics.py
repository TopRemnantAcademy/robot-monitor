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

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
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


# ---------------------------------------------------------------------------
# 订单 16 T1b：`/metrics` 的护栏证明组（robot_monitor_* 前缀）
#
# 与上面 robot_backend_* 的关系：**并存，不替换**。
#   robot_backend_* 已被 `infra/observability/rules/robot-alerts.yml` 的 PromQL
#   与 `prometheus.yml` 的抓取目标引用（订单 13/19 已落地），删掉会让现网规则静默失效。
#   robot_monitor_* 是订单 16 新增的**护栏可视化**面：AGENTS.md §3 的两条硬约束
#   （后端禁逐条 INSERT / 前端禁逐条重渲染）此前只写在文档里，没有任何代码能证明
#   它在生效。下面每个指标都对应一句可被企业验收追问的"凭什么"。
#
# 为什么不合并成一套名字：两套指标的**语义不同**，不是重命名关系。
#   robot_backend_batch_flushes_total   —— 无 label，只关心"刷了多少次"
#   robot_monitor_batch_flush_total     —— 带 reason label，要回答"是满了刷的还是超时刷的"
#   若强行合并，PromQL 侧 `sum(rate(...))` 的写法全要改，属于跨单返工。
# ---------------------------------------------------------------------------

# 队列水位。与 robot_backend_queue_depth 同源（writer.queue.qsize()），
# 但**每次抓取前都会刷新**（见 main.py 的 /metrics：拉模型下没人替我们定期 set）。
# 订单 16 验收明确要求"压测时有真实波动（不是常量 0）"，故刷新动作是硬要求。
batch_queue_size = Gauge(
    "robot_monitor_batch_queue_size",
    "asyncio.Queue 当前积压条数（0=落库跟得上 1000 msg/s；持续 >0 说明写路径是瓶颈）",
    registry=registry,
)

# 按触发原因分 label —— 这是"双重触发"机制是否按设计工作的唯一证据。
#   count   ：攒满 batch_trigger_count(500) 条触发 → 说明流入速率高，队列没空过
#   timeout ：距上批满 batch_trigger_seconds(1.0s) 触发 → 说明流入速率低，靠兜底刷
# 稳定压测时 count 应占绝大多数；若 timeout 占多数，说明没打满 1000 msg/s。
# label 值只有这两个**恒定枚举**，不存在高基数风险（对比：绝不按 robot_id 分）。
batch_flush_total = Counter(
    "robot_monitor_batch_flush_total",
    "落库批次数，按触发原因区分（count=满 500 条 / timeout=满 1s）",
    labelnames=("reason",),
    registry=registry,
)

# 落库**行数**（不是批数）。与 batch_flush_total(批数) 相乘关系：
# rows / flushes ≈ 单批平均行数，这比值是调 batch_trigger_count 的依据。
batch_rows_total = Counter(
    "robot_monitor_batch_rows_total",
    "成功写入 TimescaleDB 的遥测行数（真实入库，不含 ON CONFLICT 跳过）",
    registry=registry,
)

# MQTT 收到消息数。**刻意不带 robot_id label**：100 台 × 每台一个 series，
# 再加上 rate() 的长尾，会让 Prometheus TSDB 的倒排索引膨胀（订单 16 禁止项 1）。
# 需要按机器人下钻时走 /healthz 的即时快照或日志，不在这条长期时序里做。
mqtt_messages_total = Counter(
    "robot_monitor_mqtt_messages_total",
    "MQTT 收到的遥测消息数（不按 robot_id 分，避免 100 台高基数打爆 TSDB）",
    registry=registry,
)

# 当前 WS 连接数。Gauge 而非 Counter：大屏会断开重连，连接数可增可减。
# 用途：验证"100 台机器人"是否真的由 N 个大屏在看（连接数应为大屏台数 × 每屏 1 条），
# 以及连接泄漏排查（只增不减 = 断开的 WS 没走 finally 的计数回收）。
ws_connections = Gauge(
    "robot_monitor_ws_connections",
    "当前活跃的 /ws/robots 连接数（含未通过鉴权的握手期内连接）",
    registry=registry,
)

# /history 耗时分布。Histogram 而非 Gauge：要的是 p50/p95/p99，不是瞬时值。
# 桶边界依据：Drawer 打开是**用户交互**，1s 是体感阈值；小于 10ms 是本地缓存命中量级。
# 上限 2.5s 处若仍堆积，说明查询已在拖垮交互，需要走连续聚合表而非原始表 time_bucket。
history_latency_seconds = Histogram(
    "robot_monitor_history_latency_seconds",
    "/history 端点处理耗时（含 DB 查询，Drawer 折线 + 热力图的数据源）",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=registry,
)


def observe_flush(reason: str, rows: int) -> None:
    """记录一次落库：批次数（带 reason）+ 行数。

    `reason` 只接受 'count' / 'timeout'（由调用方判定，见 batch_writer._flush）。
    未知值不静默吞掉 —— 记成 'unknown' 让它在面板上暴露出来，
    否则拼写错误（如 'Count'）会导致面板少一块而无人察觉。
    """
    if reason not in ("count", "timeout"):
        reason = "unknown"
    batch_flush_total.labels(reason=reason).inc()
    batch_rows_total.inc(rows)


def refresh_gauges(queue_size: int, ws_conns: int) -> None:
    """在暴露 /metrics 前刷新 Gauge 瞬时值。

    为什么必须显式刷新：Prometheus 是 **pull** 模型 —— 抓取端只在 scrape 那一刻
    读值，不会替我们定期 set。Gauge 若不刷新，会永远停在上一次调用 set() 的时刻
    （表现为"常量 0"，正是订单 16 验收要排除的现象）。
    Counter / Histogram 无此问题（累加语义，调用点就在事件发生处）。
    """
    batch_queue_size.set(queue_size)
    ws_connections.set(ws_conns)
