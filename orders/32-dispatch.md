# 订单 32 — 指标命名去重：robot_monitor_* → robot_backend_*（🟢）

> 本文件同时是**交付记录的锚点**：验收以本单为准（Issue #100）。
> 分支：`feat/order-32-metrics-naming`；基线：`main@8cbe4f7`。

## 定位

你是开发工程师，证据优先。改命名前必须先把**所有引用点**找全，漏改一处就是埋雷。

## 基线

- 起点：`main`
- 交付分支：`feat/order-32-metrics-naming`

## 依据

- `orders/LEDGER.md` §8.3 第 1 条：订单 19 已落 5 个 `robot_backend_*` 指标
  （queue_depth / batch_flushes_total / telemetry_written_total / mqtt_received_total /
  mqtt_dropped_total）；订单 16 并入的分支又加了 6 个 `robot_monitor_*`
  （batch_queue_size / batch_flush_total / batch_rows_total / mqtt_messages_total /
  ws_connections / history_latency_seconds）
- 其中 `batch_queue_size` / `batch_rows_total` / `mqtt_messages_total` 与既有
  `robot_backend_*` **语义重复**（同一计数源）
- 保留：`ws_connections` / `history_latency_seconds`（订单 16 T1b 真正新增）

## 必读

1. `backend_ingestion/app/metrics.py` —— 全部指标定义
2. 全仓引用扫描（任务 T1 的第一动作就是它）
3. `infra/observability/`（含 Grafana dashboard / Prometheus rules / Alertmanager）
4. `docs/` 与 `STATUS.md` 中提及指标的段落

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

---

## 交付记录（2026-10-13，分支 `feat/order-32-metrics-naming`）

### 自证：上一轮被拒的原因

上一轮 PR #102 的 diff 只有 `orders/32-dispatch.md`（+70）与 `orders/LEDGER.md`（±1），
**零代码改动**，理由是「命名收敛已由 ISSUE #72 落地」。该理由部分成立，但漏了两件事：

1. **告警引擎侧从未被收敛** —— `alert_engine/app/metrics.py` 的在线台数指标
   仍是 `robot_alert_robots_online`，与后端 `robot_backend_robots_online` 同名问题
   完全同构，#72 的收敛范围只覆盖了后端。这是一处**真实漏改**。
2. **「旧名消失」没有运行时门禁** —— 既有 `test_observability.py:77` 用的是
   `metrics.render()`（进程内自带 registry），`verify_wiring.sh` 的旧名检查只覆盖
   6 个硬编码文件。多注册一个 Collector 即可静默复活旧名，**没有一道门禁盯真实的
   `/metrics` 字节**。

### 本单在 `main@8cbe4f7` 之上实际做的改动

| 改动 | 文件 | 性质 |
| :--- | :--- | :--- |
| 在线台数指标重命名 | `alert_engine/app/metrics.py` | **真实改名**：`robot_alert_robots_online` → `robot_alert_robots_online_gauge` |
| 引用点同步 | `infra/observability/verify_wiring.sh:56`、`infra/observability/README.md:109`、`STATUS.md:37` | 引用同步 |
| 全仓扫描门禁 | `infra/observability/verify_wiring.sh`（新增 [3.6] 段） | 旧名检查从 6 文件扩到后端/告警/基础设施/文档全仓，并逐名断言「旧名消失 + 新名在位」 |
| 运行时命名探针 | `tests/e2e/test_e2e_30k.py`（新增 + `--metric-naming-check`） | 对**真实进程**的 `/metrics` 字节断言 |
| CI 门禁 | `.cnb.yml` e2e stage | 探针接入 PR/push/crontab 共用的 e2e pipeline；HTTP 探不到则 skip 并说明，断言失败**必须红** |
| 运行时门禁说明 | `backend_ingestion/app/metrics.py` 头注释 | 记录本单补的是「运行时硬门禁」而非再改一遍名字 |

### 度量口径的澄清（不许含糊）

`main@8cbe4f7` 上 6 个 native `robot_monitor_*` 的**指标字面量早已是 `robot_backend_*`**
（由 #72 的 `3142df1` / `eeca16b` 落地），本单**不再制造一遍同样的 diff**。
本单的「实际重命名改动」落在 `alert_engine/app/metrics.py` 的在线台数指标，
并把它此前**只存在于文档与单测里的**收敛，升级为**运行时 + 全仓**两级机器门禁。
