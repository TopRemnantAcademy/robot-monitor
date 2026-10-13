# R-11 — 业务 Grafana 看板固化（robot_backend_* / robot_alert_* 可视化）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单
> 核验 + CI 双绿回收。
>
> 状态：**待执行（新需求）**。实证见 §1，非推测。

---

## 1. 背景（file:line 实证）

系统已具备完整的指标采集与供给链路，但**业务指标没有任何可视化看板**：

- **指标已导出**（后端 `backend_ingestion/app/metrics.py`）：
  - `robot_backend_queue_depth`(L84) / `robot_backend_telemetry_written_total`(L62) /
    `robot_backend_batch_flushes_total{reason}`(L78) /
    `robot_backend_mqtt_received_total`(L89) / `robot_backend_mqtt_dropped_total`(L94)
  - `robot_backend_ws_connections`(L123) / `robot_backend_ws_outbound_queue_depth`(L210) /
    `robot_backend_ws_telemetry_frames_dropped_total`(L219) /
    `robot_backend_ws_alerts_deferred_total`(L227) /
    `robot_backend_ws_slow_client_evicted_total`(L235) /
    `robot_backend_ws_hub_frames_total`(L255) / `robot_backend_ws_hub_coalesced_total`(L261)
  - `robot_backend_robots_online`(L132) / `robot_backend_history_latency_seconds`(L141) /
    `robot_backend_history_source_total{source}`(L156)
  - `robot_backend_ws_probe_alive`(L103) / `robot_backend_ws_probe_failures_total`(L108)
- **告警引擎指标已导出**（`alert_engine/app/metrics.py`）：
  - `robot_alert_emitted_total{rule_id}`(L35) / `robot_alert_suppressed_total{rule_id}`(L41) /
    `robot_alert_silenced_total{rule_id}`(L47) / `robot_alert_escalated_total{rule_id}`(L53) /
    `robot_alert_robots_online_gauge`(L71) / `robot_alert_store_errors_total`(L83)
- **Prometheus 已在抓取这两类指标**（`infra/observability/prometheus.yml:33-41`）：
  - `job_name: robot_backend` → `backend:8000/metrics`
  - `job_name: robot_alert_engine` → `alert-engine:8001/metrics`
- **Grafana 已配置自动供给**（`infra/ops/grafana/provisioning/dashboards/dashboards.yaml`）：
  - `type: file` + `path: /var/lib/grafana/dashboards` + `updateIntervalSeconds: 30` →
    放入该目录的 JSON 会被自动加载进 `RobotMonitor` 文件夹，**无需改 Grafana 配置**。
- **但现有 9 个看板全是 EMQX/MQTT broker 看板**（`infra/ops/grafana/dashboards/`）：
  `authentication / authorization / client-events / emqx-self-details / emqx-self-overview /
  messages / overview / rule-engine-count / rule-engine-rate`。
  → **`grep -rl "robot_backend\|robot_monitor\|robots_online\|history_latency" infra/ops/grafana/dashboards/` 返回 NONE**：
  业务指标（`robot_backend_*` / `robot_alert_*`）**完全无看板**。运维只能 `curl :8011/metrics`
  读裸文本，无法在 Grafana 上直观看到落库吞吐、WS 背压、在线率、history 延迟、告警发射/抑制。

**结论**：数据链路（导出→抓取→供给）已通，**唯一缺口是把业务指标画成看板 JSON**。
这是低风险、纯新增、codebuddy 可直接落地的需求。

---

## 2. 目标

在 `infra/ops/grafana/dashboards/` 新增 **业务看板 JSON**，让 `robot_backend_*` 与
`robot_alert_*` 指标在 Grafana `RobotMonitor` 文件夹下可视化，覆盖核心 SLO：

1. **落库健康**：吞吐 `telemetry_written_total`、队列积压 `queue_depth`、批刷新 `batch_flushes_total{reason}`、MQTT `received/dropped`。
2. **WS 实时性**：连接数 `ws_connections`、出站队列 `ws_outbound_queue_depth`、丢帧 `ws_telemetry_frames_dropped_total`、慢客户端摘除 `ws_slow_client_evicted_total`、中枢合并 `ws_hub_frames_total`/`ws_hub_coalesced_total`、探针 `ws_probe_alive`/`ws_probe_failures_total`。
3. **在线态**：`robot_backend_robots_online` 与 `robot_alert_robots_online_gauge`（两口径对照）。
4. **查询延迟**：`robot_backend_history_latency_seconds` p95 + 数据源 `history_source_total{source}`。
5. **告警流**：`robot_alert_emitted_total{rule_id}` / `suppressed_total` / `silenced_total` / `escalated_total` / `store_errors_total`。

---

## 3. 范围边界（明确不做）

- **不修改** `backend_ingestion/app/metrics.py` / `alert_engine/app/metrics.py`：指标命名已锁定（R-07/订单 32 收敛），看板只能**消费**既有名，不得新增/改名。
- **不修改** `prometheus.yml` / `dashboards.yaml` / `datasources.yaml`：供给链路已通，只需新增看板文件。
- **不引入** 新 exporter / 新数据源。
- **不动** EMQX 既有 9 个看板（保留）。
- 前端（`frontend_dashboard/`）**不在此次范围**：本需求是运维侧 Grafana 看板，与 R-01 大屏互补不重叠。

---

## 4. 任务分解

### T1 — 后端业务看板 `robot-backend.json`
- 放入 `infra/ops/grafana/dashboards/robot-backend.json`。
- 面板（引用上述 `robot_backend_*` 真名，逐个与 `metrics.py` grep 核对，不得捏造）：
  1. 落库吞吐（rate `telemetry_written_total`）、队列积压（`queue_depth`，配 `>5000` 告警线）、批刷新（`batch_flushes_total` by `reason`）。
  2. MQTT 接收/丢弃（`mqtt_received_total` / `mqtt_dropped_total`，`dropped` 非 0 即异常）。
  3. WS 连接数（`ws_connections`）、出站队列（`ws_outbound_queue_depth`）、丢帧/慢客户端摘除计数器。
  4. WS 中枢合并率（`ws_hub_coalesced_total` / `ws_hub_frames_total`，看合并收益）。
  5. 在线率（`robots_online`）、history p95 延迟（`history_latency_seconds`，histogram_quantile(0.95)）、数据源占比（`history_source_total` by `source`）。
  6. WS 探针存活/失败（`ws_probe_alive` / `ws_probe_failures_total`）。
- `templating`：可选 `datasource` 变量；`rule_id` 不在后端看板（那是告警引擎）。

### T2 — 告警引擎看板 `robot-alerts.json`
- 放入 `infra/ops/grafana/dashboards/robot-alerts.json`。
- 面板（引用 `robot_alert_*` 真名，`metrics.py:34-83`）：
  1. 告警发射按规则（`emitted_total{rule_id}`，按 `rule_id` 分组）。
  2. 抑制 / 静音 / 升级（`suppressed_total` / `silenced_total` / `escalated_total`）。
  3. 在线台数（`robots_online_gauge`，与后端 `robots_online` 对照）。
  4. 落库错误（`store_errors_total`，非 0 即告警链路故障）。

### T3 — 自检
- 启动 observability 栈（`infra/observability/docker-compose.observability.yml` + grafana 挂载 `infra/ops/grafana`），
  确认 `RobotMonitor` 文件夹出现两个新看板，且在有流量时面板显示非 0 序列。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 看板文件存在 | `infra/ops/grafana/dashboards/robot-backend.json` 与 `robot-alerts.json` 均存在 | `ls` |
| V2 | 指标名真实 | 两个 JSON 内所有 `expr` 引用的指标名均在 `metrics.py` 中 grep 命中（无拼写/捏造） | 脚本 grep 校验（可作为 CI 门禁，可选） |
| V3 | 自动加载 | observability 栈启动后 `curl 'grafana:3000/api/search?folderUid=robot-monitor'` 返回两个新 UID | Grafana HTTP API |
| V4 | 有数据 | 在 `test_integration.sh` 跑过一轮（有 30k 落库 + 告警）后，面板显示非 0 序列 | 看板截图 / API 取 panel 数据 |
| V5 | 不回归 | 现有 9 个 EMQX 看板仍正常；`metrics.py` / `prometheus.yml` / `dashboards.yaml` 无任何改动（`git diff` 白名单仅 2 个新 JSON） | `git diff --stat` |

---

## 6. 约束 / 禁止项

- 禁止新增/重命名 `robot_backend_*` / `robot_alert_*`（命名是 R-07/订单 32 锁定的单一事实来源）。
- 禁止引用不存在的指标名（捏造 → V2 失败）。
- 禁止改 `prometheus.yml` / `dashboards.yaml` / `datasources.yaml`（供给已通）。
- 禁止删/改 EMQX 既有看板。
- 禁止为通过而放宽：面板 `expr` 必须贴真实指标语义（如 `queue_depth` 告警线沿用 R-09 §2 的 `>5000`）。

---

## 7. 交付物（codebuddy 产出，主代理回收核验）

- `infra/ops/grafana/dashboards/robot-backend.json`
- `infra/ops/grafana/dashboards/robot-alerts.json`
- （可选）校验脚本：grep 两个 JSON 的 `expr` 指标名是否均在 `metrics.py` 命中。
- PR 的 `git diff` 必须**仅含这两个新文件**（白名单），不得夹带 `metrics.py` / 业务代码改动。
