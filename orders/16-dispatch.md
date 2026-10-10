# 派工单：订单 16 — 可观测性三件套（"完美接应"的物理基础）

> 依据：`docs/design/ENTERPRISE-GAP-ANALYSIS.md` §2.4 / §3.2（缺口 E2/E3）
> 触发方式：`post-issue-comment`（本文件不含触发词）

---

## 0. 为什么这是"接应"的核心，而不是加页面

AGENTS.md §3 立了两条硬护栏：

| 护栏 | 原文位置 | 当前能否验证 |
|---|---|---|
| 后端禁逐条 INSERT，批量满 500 条或 1 秒 | `backend_ingestion/app/config.py:27-29` | ❌ 无 metrics |
| 前端禁每条 WS 消息重渲染，必须 100ms 节流 | `frontend_dashboard/src/store/robotStore.ts:4-5,84` | ❌ 无任何计数 |

**事实**：护栏写在纸上，**没有任何代码能证明它正在生效**。
企业验收必问「你凭什么说没重渲染 1000 次/秒」——目前只能嘴说。

---

## 1. 任务

### T1 — 后端 `/metrics`（Prometheus 文本格式）
用 `prometheus-client`（已在 `external/client_python`）。**禁止引入重框架**。

必须暴露（命名用 `robot_monitor_` 前缀）：
| 指标 | 类型 | 说明 |
|---|---|---|
| `robot_monitor_batch_queue_size` | Gauge | 队列水位（`batch_queue_maxsize` = 20000，见 `config.py:29`） |
| `robot_monitor_batch_flush_total` | Counter | 落库批次数（按触发原因 label：`count` / `timeout`） |
| `robot_monitor_batch_rows_total` | Counter | 落库行数 |
| `robot_monitor_mqtt_messages_total` | Counter | MQTT 收到消息数（按 `robot_id` label 会爆基数，**不按 robot_id 分**） |
| `robot_monitor_ws_connections` | Gauge | 当前 WS 连接数 |
| `robot_monitor_history_latency_seconds` | Histogram | `/history` 耗时 |

`GET /metrics` 免鉴权（与 `/healthz` 同级）。

### T2 — 前端自报重渲染（本单的**关键交付**）
- 新增 `frontend_dashboard/src/lib/observability.ts`：
  - 计数器 `telemetryReceived`（WS 收到消息数）
  - 计数器 `storeCommits`（`applyTelemetryBatch` 实际调用次数）
  - 计数器 `cardRenders`（`FleetOverviewCards` 渲染次数，用 `useRef` 自增，**不得触发额外渲染**）
- 每 5 秒 `console.info` 一行结构化摘要（**只打印，不上报外网**）：
  `{wsMsgs, storeCommits, cardRenders, ratio}`
- **验收语义**：稳定运行时 `wsMsgs / storeCommits` 应接近 **10**（1000 msg/s ÷ 100 次/s），即节流生效

### T3 — OpenTelemetry 链路（MQTT → 后端 → DB）
- Python：`opentelemetry-instrumentation-fastapi` + `opentelemetry-instrumentation-asyncpg`
- 导出目标从 `config.py` 读（默认关闭，`OTEL_EXPORTER_OTLP_ENDPOINT` 为空则 no-op）
- **禁止**因 OTel 未配置就启动失败（必须可降级）

### T4 — 结构化日志
- 后端日志改 JSON 单行（含 `ts/level/logger/msg/robot_id?`）
- 便于订单 07 的 `openobserve`/`loki` 直接采集

---

## 2. 验收标准

- [ ] `GET /metrics` 返回 Prometheus 文本，含 T1 全部 6 个指标名
- [ ] 压测时 `robot_monitor_batch_queue_size` 有真实波动（不是常量 0）
- [ ] 前端控制台稳定输出 `wsMsgs/storeCommits ≈ 10`（**贴真实截图或 console 文本**）
- [ ] `cardRenders` 增长速率 ≈ `storeCommits`（不得 ≈ `wsMsgs`）
- [ ] `OTEL_EXPORTER_OTLP_ENDPOINT` 未设时服务正常启动
- [ ] 日志为单行 JSON，可被 `jq` 解析
- [ ] `tsc --noEmit` 0 错误；`vite build` RC=0
- [ ] 后端 `import app.main` 成功

## 3. 禁止项

1. **禁止**按 `robot_id` 做 Prometheus label（100 台 × 高基数会压垮 TSDB）
2. **禁止**让 `/metrics` 走鉴权（Prometheus 抓取会失败）
3. **禁止**用上报到外网的服务（企业内网无外网出口）
4. **禁止**为计数而引入状态管理重构（本单只加旁观者，不改数据流）
5. **禁止**伪造 console/metrics 输出
6. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；失败则**原文报告**

## 4. 冲突提示

- 与订单 11 同改 `backend_ingestion/app/main.py`：本单加**中间件 + /metrics 路由**，订单 11 加 `Depends`。合并时**两者都要保留**。
- 与订单 11 同改 `useRobotWebSocket.ts`：订单 11 加 token 拼接，本单加计数。合并保留两者。
