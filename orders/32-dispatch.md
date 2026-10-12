# 订单 32 — 指标命名去重：robot_monitor_* → robot_backend_*（🟢）

## 定位

你是开发工程师，证据优先。改命名前必须先把**所有引用点**找全，漏改一处就是埋雷。

## 基线

- 起点：`main`
- 交付分支：`feat/order-32-metrics-naming`

## 依据

- `orders/LEDGER.md` §8.3 第 1 条：订单 19 已落 5 个 `robot_backend_*` 指标（queue_depth / batch_flushes_total / telemetry_written_total / mqtt_received_total / mqtt_dropped_total）；订单 16 并入的分支又加了 6 个 `robot_monitor_*`（batch_queue_size / batch_flush_total / batch_rows_total / mqtt_messages_total / ws_connections / history_latency_seconds）
- 其中 `batch_queue_size` / `batch_rows_total` / `mqtt_messages_total` 与既有 `robot_backend_*` **语义重复**（同一计数源）
- 保留：`ws_connections` / `history_latency_seconds`（订单 16 T1b 真正新增）

## 必读

1. `backend_ingestion/app/metrics.py` —— 全部指标定义
2. 全仓引用扫描（任务 T1 的第一动作就是它）
3. `infra/observability/`（含 Grafana dashboard / Prometheus rules / Alertmanager）
4. `docs/` 与 STATUS.md 中提及指标的段落

## 任务分解

### T1 引用扫描（先做，结果贴进 PR）

```
grep -rn "robot_monitor_" --include="*.py" --include="*.json" --include="*.yml" --include="*.yaml" --include="*.md" .
```
把命中清单（file:line）全量贴进 PR 描述，作为改前改后的对照。

### T2 合并命名

- 语义重复的三个 → 直接合并到既有 `robot_backend_*` 计数器（**删重名定义，不许并存**）
- 真正新增的两个 → 改名为 `robot_backend_ws_connections` / `robot_backend_history_latency_seconds`
- 同步更新所有引用：代码 + dashboard JSON + Prometheus rules + 文档

### T3 校验

- 提供一次**可复跑**的端点校验（起服务或用测试客户端打 `/metrics`，断言旧名不存在、新名存在）
- CI 跑不了的部分按既有约定 skip 并打印原因（不得伪装通过）

## 验收标准（可判定）

- [ ] `grep -rn "robot_monitor_"` 在 `backend_ingestion/` `alert_engine/` `infra/` `docs/` 上 **0 命中**（白名单：本单文件与 LEDGER 的历史记录段落）
- [ ] `/metrics` 实际输出含 5+2 个 `robot_backend_*`，不含任何 `robot_monitor_*`
- [ ] `python -m pytest -q` 全绿
- [ ] dashboard/告警规则 JSON 语法有效（`python -c "import json;json.load(open(...))"`）
- [ ] `orders/32-dispatch.md` 一并提交进分支

## 禁止项

1. 禁止用「两套语义不同」为由保留并存（重复计数源会让面板双计数，比改名更糟）
2. 禁止只改代码不改 dashboard（漏改 = 面板静默变空）
3. 禁止改 `pytest.ini` testpaths
4. 禁止弱化既有断言
5. 禁止删除已在用的指标而不同步补偿面板查询

## 交付方式

分支 `feat/order-32-metrics-naming` → PR，描述贴改前/改后的 grep 清单与 `/metrics` 实测片段。

## 失败上报

原文粘贴错误；禁止静默失败。
