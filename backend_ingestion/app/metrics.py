"""Prometheus 指标暴露（后端采集层）。

为什么需要这个文件（此前是「下了没接」的典型）：
  `prometheus/client_python` 在上一轮已 clone 到 `external/`，
  但 `backend_ingestion/` 里**没有任何一处 import** ——
  没有指标导出，配置再多 Prometheus 抓取目标也只会拿到 `up=0`。

  本模块把已经在 `/healthz` 里暴露的运行时计数**同时**导出为 Prometheus 指标，
  于是 `up{job="robot_backend"}` 与 `robot_backend_queue_depth` 等
  在 `infra/observability/prometheus.yml` 里声明的指标**真实存在**。

指标命名对齐 `infra/observability/rules/robot-alerts.yml` 的 PromQL：
  robot_backend_queue_depth          （Gauge，规则里用 `> 5000`）
  robot_backend_mqtt_dropped_total   （Counter，规则里用 increase(...[1m])）

本模块只管「指标定义」，不负责采集。WS 相关有两个来源，**不要混淆**：
  - `robot_backend_ws_connections` —— 服务端当前连接数（订单 16 T1b），
    由 `main.py` 的 `ws_robots` 连接/断开时增减；
  - `robot_backend_ws_probe_alive` / `robot_backend_ws_probe_failures_total`
    —— WS **合成探针**（客户端视角），由 `app/ws_probe.py` 的循环更新。
两者是互补而非重复：连接数为 1 只证明「有个客户端挂着」，
探针为 1 才证明「这条连接真的还能收到首帧 snapshot」。

命名单一来源（ISSUE #72）：
  历史上订单 16 的实现分支另立了一套 `robot_monitor_*` 前缀（6 个指标），与订单 19
  已落地的 `robot_backend_*` **并存**，其中 3 个是同一计数源的重复指标。本模块现已
  收敛为 `robot_backend_*` **唯一命名**：
    - `robot_monitor_batch_queue_size`         → 删除（同源：`robot_backend_queue_depth`）
    - `robot_monitor_batch_rows_total`         → 删除（同源：`robot_backend_telemetry_written_total`）
    - `robot_monitor_mqtt_messages_total`      → 删除（同源：`robot_backend_mqtt_received_total`）
    - `robot_monitor_batch_flush_total{reason}`→ 改名并入 `robot_backend_batch_flushes_total{reason}`
    - `robot_monitor_ws_connections`           → 改名为 `robot_backend_ws_connections`
    - `robot_monitor_history_latency_seconds`  → 改名为 `robot_backend_history_latency_seconds`
  被删的 3 个之所以能删，是因为 prometheus_client 的 Counter/Gauge 是**幂等累加/覆盖**
  语义：删掉重复定义后，同一事件只经一个计数器表达，数值不会丢、也不会双份。
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
# 按触发原因分 label —— 这是「双重触发（满 500 条 / 满 1s）」是否按设计工作的唯一证据。
#   count   ：攒满 batch_trigger_count(500) 条触发 → 说明流入速率高，队列没空过
#   timeout ：距上批满 batch_trigger_seconds(1.0s) 触发 → 说明流入速率低，靠兜底刷
# 稳定压测时 count 应占绝大多数；若 timeout 占多数，说明没打满 1000 msg/s。
# label 值只有这两个**恒定枚举**（外加脏值归一后的 'unknown'），不存在高基数风险
# （对比：绝不按 robot_id 分）。
#
# ISSUE #72：本指标吸收了原 `robot_monitor_batch_flush_total` —— 后者带 reason label、
# 后者无 label，是「同一刷批事件」的两份时序。合并后保留 label 能力（信息不降级），
# 只留一个名字（PromQL/面板/告警无需再维护两套）。metric 名沿用现网已有的
# `robot_backend_batch_flushes_total`，故 PromQL 侧 `sum(rate(...))` 写法无需改动。
batch_flushes = Counter(
    "robot_backend_batch_flushes_total",
    "BatchWriter 的刷批次数，按触发原因区分（count=满 500 条 / timeout=满 1s）",
    labelnames=("reason",),
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

# ---- WS 合成探针（缺口 G-3「看板假的绿」）----
# 注意：这两个是**探针视角**指标，与 `robot_backend_ws_connections`（服务端连接数，
# 订单 16 T1b）语义不同，禁止互相覆盖或复用一个名字。
ws_probe_alive = Gauge(
    "robot_backend_ws_probe_alive",
    "WS 合成探针最近一次探活结果（1=握手并收到 snapshot 首帧 / 0=失败）",
    registry=registry,
)
ws_probe_failures = Counter(
    "robot_backend_ws_probe_failures_total",
    "WS 合成探针累计失败次数（>0 说明客户端视角的实时流断过）",
    registry=registry,
)

# ---- 订单 16 T1b：护栏可视化面（ISSUE #72 已并入 robot_backend_* 单一命名）----
# AGENTS.md §3 的两条硬约束（后端禁逐条 INSERT / 前端禁逐条重渲染）此前只写在文档里，
# 下面每个指标都对应一句可被企业验收追问的"凭什么"。
# 与上方指标无重复：ws_connections / history_latency_seconds 是本单真正新增的能力。

# 当前 WS 连接数。Gauge 而非 Counter：大屏会断开重连，连接数可增可减。
# 用途：验证"100 台机器人"是否真的由 N 个大屏在看（连接数应为大屏台数 × 每屏 1 条），
# 以及连接泄漏排查（只增不减 = 断开的 WS 没走 finally 的计数回收）。
# 刷新动作在 main.py 的 /metrics（pull 模型下没人替我们定期 set）。
ws_connections = Gauge(
    "robot_backend_ws_connections",
    "当前活跃的 /ws/robots 连接数（含未通过鉴权的握手期内连接）",
    registry=registry,
)

# /history 耗时分布。Histogram 而非 Gauge：要的是 p50/p95/p99，不是瞬时值。
# 桶边界依据：Drawer 打开是**用户交互**，1s 是体感阈值；小于 10ms 是本地缓存命中量级。
# 上限 2.5s 处若仍堆积，说明查询已在拖垮交互，需要走连续聚合表而非原始表 time_bucket。
history_latency_seconds = Histogram(
    "robot_backend_history_latency_seconds",
    "/history 端点处理耗时（含 DB 查询，Drawer 折线 + 热力图的数据源）",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=registry,
)


def render() -> tuple[bytes, str]:
    """返回 (body, content_type)，供 FastAPI 的 /metrics 端点直接使用。"""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def observe_flush(reason: str, rows: int) -> None:
    """记录一次落库：批次数（带 reason）+ 行数。

    `reason` 只接受 'count' / 'timeout'（由调用方判定，见 batch_writer._flush）。
    未知值不静默吞掉 —— 记成 'unknown' 让它在面板上暴露出来，
    否则拼写错误（如 'Count'）会导致面板少一块而无人察觉。
    """
    if reason not in ("count", "timeout"):
        reason = "unknown"
    batch_flushes.labels(reason=reason).inc()
    telemetry_written.inc(rows)


def refresh_gauges(queue_size: int, ws_conns: int) -> None:
    """在暴露 /metrics 前刷新 Gauge 瞬时值。

    为什么必须显式刷新：Prometheus 是 **pull** 模型 —— 抓取端只在 scrape 那一刻
    读值，不会替我们定期 set。Gauge 若不刷新，会永远停在上一次调用 set() 的时刻
    （表现为"常量 0"，正是订单 16 验收要排除的现象）。
    Counter / Histogram 无此问题（累加语义，调用点就在事件发生处）。
    """
    queue_depth.set(queue_size)
    ws_connections.set(ws_conns)
