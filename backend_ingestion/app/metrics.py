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

命名单一来源（订单 32 / ISSUE #100，合并 ISSUE #72 的收敛动作）：
  历史上订单 16 的实现分支另立了一套 `robot_monitor_*` 前缀（6 个指标），与订单 19
  已落地的 `robot_backend_*` **并存**，其中 3 个是同一计数源的重复指标。本模块收敛为
  `robot_backend_*` **唯一命名**。下表 12 处旧名（6 个 native + 6 个第二波）已作废；
  本单（订单 32）在 `main@8cbe4f7` 之上补的是**运行时硬门禁**：
  此前"旧名已消失"只有注释与守卫单测背书，
  而单测用的是自带 registry 的 render()，并不等于真实 `/metrics` 的字节。
    - `robot_monitor_batch_queue_size`         → 删除（同源：`robot_backend_queue_depth`）
    - `robot_monitor_batch_rows_total`         → 删除（同源：`robot_backend_telemetry_written_total`）
    - `robot_monitor_mqtt_messages_total`      → 删除（同源：`robot_backend_mqtt_received_total`）
    - `robot_monitor_batch_flush_total{reason}`→ 改名并入 `robot_backend_batch_flushes_total{reason}`
    - `robot_monitor_ws_connections`           → 改名为 `robot_backend_ws_connections`
    - `robot_monitor_history_latency_seconds`  → 改名为 `robot_backend_history_latency_seconds`
  被删的 3 个之所以能删，是因为 prometheus_client 的 Counter/Gauge 是**幂等累加/覆盖**
  语义：删掉重复定义后，同一事件只经一个计数器表达，数值不会丢、也不会双份。

第二波收敛（2026-10-11，CI 实录）：#73/#76/#77 的回收分支基于旧 main 开发，
  彼此不知道对方的命名约束，合并后 6 个旧前缀指标再次入库，被 #72 的守卫测试
  （FORBIDDEN_MONITOR_PREFIX 行首前缀断言）当场拦下 —— 这正是守卫存在的意义。
    - `robot_monitor_robots_online`                     → `robot_backend_robots_online`
    - `robot_monitor_history_source_total`              → `robot_backend_history_source_total`
    - `robot_monitor_ws_outbound_queue_depth`           → `robot_backend_ws_outbound_queue_depth`
    - `robot_monitor_ws_telemetry_frames_dropped_total` → `robot_backend_ws_telemetry_frames_dropped_total`
    - `robot_monitor_ws_alerts_deferred_total`          → `robot_backend_ws_alerts_deferred_total`
    - `robot_monitor_ws_slow_client_evicted_total`      → `robot_backend_ws_slow_client_evicted_total`
  这些指标尚未被任何面板/告警规则引用，纯改名无下游兼容负担。
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

# 当前在线台数（ISSUE #77）。语义是「距最后一次收到遥测 ≤ robot_offline_threshold_s」，
# 而不是「status_code != 0」—— 停报的机器人 status_code 会冻在最后一帧，
# 用它统计会稳定虚高。刷新点：`/healthz`（编排以秒级频率打，顺带维护，不另起定时器）。
robots_online = Gauge(
    "robot_backend_robots_online",
    "在线机器人数（now - last_seen <= 阈值，ISSUE #77；停报者不计入）",
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

# /history 的数据源计数（ISSUE #76）。
#
# 为什么必须有：改查连续聚合后 p95 会下降，但**「变快」单独看无法自证**
# —— 空结果、缓存、甚至查询写错都会让耗时变短。把数据源拆成 cagg/raw 两个
# series 后，p95 下降与 `source="cagg"` 占比上升必须同时成立才算真的生效；
# 反之若 `source="raw"` 长期占多数，说明聚合没跟上（刷新策略或调度有问题），
# 这正是回归该发现的那类故障，而不是靠人眼盯。
# label 只有 2 个取值，基数可控（对比 mqtt_received 刻意不带 robot_id 的理由）。
history_source = Counter(
    "robot_backend_history_source_total",
    "/history 实际命中数据源：cagg=连续聚合 robot_history_10s / raw=原始表回退",
    ("source",),
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


# ---------------------------------------------------------------------------
# ISSUE #73：WS 出站背压 + 心跳
#
# 为什么单列一组而不是复用 robot_monitor_ws_connections：
# 连接数回答"几台大屏挂着"，**不回答"它们还跟得上吗"**。一个慢客户端
# （移动网络 / 标签页被冻结）连接数照样 +1，却把 1000 msg/s 的出站帧全积在
# 自己的队列里。下面三个指标就是"谁在拖后腿"的可观测面：
#   - outbound_queue_depth  Gauge   按连接聚合的出站积压（持续 >0 = 有人消费不动）
#   - telemetry_frames_dropped_total  Counter 被**丢弃**的遥测帧（内存有界的代价）
#   - alerts_deferred_total           Counter 因队列满而顺延的告警（>0 = 病态，
#                                            0 是"告警不可丢"约束生效的证据）
# 刻意不带 connection label：连接是短生命周期对象，按连接分 label 会让
# Prometheus 时序无限增长（每次重连都是新 series）。需要下钻时看日志。
# ---------------------------------------------------------------------------

# 出站积压（每台机器一条 WS 连接，这里取**所有连接的合计**）。
outbound_queue_depth = Gauge(
    "robot_backend_ws_outbound_queue_depth",
    "所有 /ws/robots 连接出站队列的积压帧数合计（持续 >0 说明有慢客户端）",
    registry=registry,
)

# 被丢弃的遥测帧。**遥测可丢、告警不可丢**（ISSUE #73 禁止项第三条）：
# 这个计数涨是设计内的背压行为，不是故障；它涨而 alerts_deferred_total 恒 0，
# 恰好证明分级处置按预期工作。
telemetry_frames_dropped = Counter(
    "robot_backend_ws_telemetry_frames_dropped_total",
    "因出站队列满而被丢弃的遥测帧数（丢最旧，慢客户端的背压代价）",
    registry=registry,
)

# 顺延的告警。设计目标恒为 0：告警溢出时队列允许扩容（见 ws_backpressure），
# 只有几千条告警积压这种病态情形才会让这个数 >0。
alerts_deferred = Counter(
    "robot_backend_ws_alerts_deferred_total",
    "因出站队列满而顺延的告警帧数（应为 0；>0 说明告警积压，需扩容或摘连接）",
    registry=registry,
)

# 被摘除的慢/半开连接数。与连接数 Gauge 互补：连接数掉下来是**结果**，
# 这个计数是**原因**——运维据此区分"大屏正常关闭"与"被服务端踢掉"。
ws_slow_client_evicted = Counter(
    "robot_backend_ws_slow_client_evicted_total",
    "因出站队列持续满或心跳超时被主动 close 的慢客户端连接数",
    registry=registry,
)

# ---------------------------------------------------------------------------
# ISSUE #71：WS 出站 fan-out 中枢（服务端广播合并）
#
# 为什么单列一组：#73 的指标回答"单条连接跟不跟得上"，
# 这两个指标回答"**服务端**是否还在随客户端数线性膨胀"：
#   - frames_total{kind}    实际**发送**的 WS 帧总数（含每连接各一份）。
#     改造前 ≈ N × 1000/s（每连接独立订阅 + 逐条 send）；
#     改造后 ≈ N × (节拍 × 机器人数)，序列化全进程只做一份。
#   - coalesced_total       在节拍内被**合并掉**的遥测条数（覆盖写掉的中间帧）。
#     它 > 0 才证明「每台机器人只保留最新一帧」真的在生效；
#     恒为 0 说明节拍没起作用（收到的每条都被单独发了）。
# 不设 ws_hub_clients：连接注册与 ws_robots 的 _ws_active 一一对应，
# 再发一个同源 Gauge 就是 #72 刚清完的双轨命名故技复萌。
# ---------------------------------------------------------------------------
ws_hub_frames_total = Counter(
    "robot_backend_ws_hub_frames_total",
    "WS 出站中枢实际发送的帧数，按 kind 区分（telemetry=合并后每机器人一帧 / alert=逐条不合并）",
    labelnames=("kind",),
    registry=registry,
)
ws_hub_coalesced_total = Counter(
    "robot_backend_ws_hub_coalesced_total",
    "被节拍合并掉的遥测帧数（本节拍收到数 - 实际扇出数；>0 = 每机器人只留最新一帧生效）",
    registry=registry,
)

# ---------------------------------------------------------------------------
# Benchmark C: 오프라인 버퍼（store-and-forward）
#
# TimescaleDB 장애 시 버퍼링된 후 DB 복구 시 재생되는遥测 행 수.
#   - telemetry_buffered_total       : 버퍼에 보관된 행 수（누적）
#   - telemetry_buffered_replayed_total : 재생되어 실제 재투입된 행 수（누적）
# 둘의 차이가 지속적으로 > 0 이면「버퍼는 쌓이는데 재생이 안 된다」= Redis↔DB
# 불일치, 혹은 재생 루프가 죽은 것. 이 지표가 없으면「버퍼가 쌓여도 대시보드는
# 여전히 초록」이라는 가짜 녹색을 막을 수 없음（G-4 동일한 맥락）.
# ---------------------------------------------------------------------------
telemetry_buffered = Counter(
    "robot_backend_telemetry_buffered_total",
    "DB 쓰기 실패로 오프라인 버퍼(Redis 리스트)에 보관된遥测 행数（재생 대기）",
    registry=registry,
)
telemetry_buffered_replayed = Counter(
    "robot_backend_telemetry_buffered_replayed_total",
    "오프라인 버퍼에서 재생되어 writer 에 재투입된遥测 행数",
    registry=registry,
)
