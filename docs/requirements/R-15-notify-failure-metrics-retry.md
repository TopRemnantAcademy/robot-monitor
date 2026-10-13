# R-15 — 告警通知投递失败：指标 + 有界重试（可观测盲区）

> **@//codebuddy/** — 本需求由本地（主代理）撰写，指派 codebuddy NPC 读仓执行。
> 本地**只写需求规格、不写业务修复代码**；codebuddy 落地后主代理按两点 diff 白名单核验 + CI 双绿回收。
>
> 状态：**待执行（可观测 + 韧性，低-中风险）**。实证见 §1，非推测。
> ⚠️ 硬门槛：**通知失败绝不能阻塞/丢失告警落库**（落库先行，通知尽力而为）。

---

## 1. 背景（file:line 实证）

通知链路现状：
- `alert_engine/app/main.py:216-217`：`if _notifier is not None: await _notifier.send(alert)` —— 通知在主链路上**直接 await**；
- `alert_engine/app/notifiers/webhook.py:115`：`log.exception("webhook notify failed (%s): %s/%s", ...)` —— 投递失败**只记日志**，随后异常被丢弃；
- `alert_engine/app/metrics.py` 现有指标（实证，逐行）：`alerts_emitted`(`:34`)、`alerts_suppressed`(`:40`)、`alerts_silenced`(`:46`)、`alerts_escalated`(`:52`)、`robots_online_gauge`(`:70`)、`store_errors`(`:82`)。
  **其中没有任何"通知投递失败"相关指标。**

**故障模式（真）**：webhook 配错 / 钉钉或企微接口限流 / 网络不通时，告警**已成功落库并广播**，但**通知静默丢失**——运维收不到，且因为没有计数器，**Prometheus 无法告警、无人知晓**。这是典型可观测盲区（与 GAP-3 描述一致）。

对照：`store.py:82` 已有 `store_errors` Counter 用于"落库失败"，说明项目是认可"失败要计数"的；唯独通知环节缺这一环。

---

## 2. 目标

- 新增 **`robot_alert_notify_failed_total{reason}`** Counter（`reason` 区分失败原因，如 `http_error` / `timeout` / `network` / `config` 等），在 `/metrics` 可见，使"通知丢失"可被 Prometheus 告警；
- 通知失败**有界重试**（指数退避、次数上限、总耗时上限），且**不阻塞主链路**；
- 保证：**通知失败不影响告警落库/广播**（落库先行语义不变）。

---

## 3. 范围边界（明确不做）

- **不阻塞**告警落库与 WS 广播（`:216-217` 之前的流程保持）；
- **不改** 告警规则、去重、静默、升级逻辑；
- **不改** 现有指标名/前缀（沿用 `robot_alert_*`，与 `metrics.py:34-82` 一致）；
- **不引入** 新消息队列/外部依赖（重试用 `asyncio` 即可）；
- 不要求"投递一定成功"（外部系统故障无法内部保证），只要求**失败可见 + 有限重试**；
- 不改 `backend_ingestion`（本需求只在 `alert_engine`）。

---

## 4. 任务分解

### T1 — 指标（`alert_engine/app/metrics.py`）
- 新增 `notify_failed_total = Counter("robot_alert_notify_failed_total", "...", ["reason"])`（变量名/命名风格对齐 `:34-82` 现有写法）。
- 可选（推荐）：再加一个 `notify_sent_total` 便于算失败率；若加，同样用 `robot_alert_*` 前缀。

### T2 — 通知调用处计数（`alert_engine/app/main.py`）
- 把 `:216-217` 的 `await _notifier.send(alert)` 包进 try/except：
  - 成功 → 计数 sent（若实现）；
  - 失败 → **分类 reason** 后 `notify_failed_total.labels(reason=...).inc()` + `log.exception`（保留现有日志，补指标）。
- **关键**：异常仍在本处终结（不得向上传播），保证落库/广播链路不受影响。

### T3 — 有界重试（可选但推荐）
- 在通知处加**有界**重试：最多 N 次（建议 3）、指数退避、总耗时上限（建议 ≤5s），失败后按 T2 计数并放弃。
- **禁止无限重试 / 禁止阻塞主链路**（若重试会拖慢告警处理，必须放在后台任务或严格限时）。

### T4 — 失败原因分类（`alert_engine/app/notifiers/webhook.py`）
- 让 `webhook.py` 抛出的异常带**可分类的 reason**（如 HTTP 状态码非 2xx → `http_error`；超时 → `timeout`；连接失败 → `network`），供 T2 打标签。
- 保留 `:115` 的 `log.exception`（不删除既有日志）。

### T5 — 回归测试
- 单测：mock 通知器抛错 → 断言 `notify_failed_total{reason=...}` +1、主链路**不抛异常**、落库逻辑未被跳过；
- 单测：mock 通知器成功 → 断言失败计数不增；
- 既有 `alert_engine` 单测全绿。

---

## 5. 验收标准（可判定）

| # | 用例 | 期望 | 判定依据 |
|---|---|---|---|
| V1 | 指标存在 | `robot_alert_notify_failed_total{reason}` 在 `/metrics` 可见 | 起服务 `curl /metrics` 或单测断言注册表 |
| V2 | 失败计数 | mock 通知失败 → 该指标 `+1`，且 reason 分类正确 | 新单测 |
| V3 | 不阻塞主链路 | 通知失败时告警**仍完成落库/广播**，异常不外溢 | 新单测 + 读 `:216-217` |
| V4 | 有界重试（若实现） | 重试次数/耗时有上限，不无限、不拖垮处理 | 新单测 |
| V5 | 既有契约不动 | `alert_engine` 既有单测全绿；指标命名沿用 `robot_alert_*` | CI + grep 命名 |
| V6 | CI 双绿 | `pipeline-1(test)` + `pipeline-2(e2e-30k-landing)` 均 success | `cnb pulls list-pull-commit-statuses` |

---

## 6. 约束 / 禁止项（白名单）

- `git diff` 白名单：`alert_engine/app/main.py` + `alert_engine/app/metrics.py` + `alert_engine/app/notifiers/webhook.py` + `alert_engine` 测试文件。
- **禁止**让通知失败阻塞或回滚告警落库/广播（硬门槛）。
- **禁止**无限重试 / 无上限等待（必须有次数与耗时上限）。
- **禁止**改动现有指标名与 `robot_alert_*` 前缀规范。
- **禁止**删除 `:115` 的既有 `log.exception`。
- 不得夹带 `backend_ingestion` / 前端 / `.cnb.yml` 改动。

---

## 7. 交付物

- `alert_engine/app/metrics.py`：新增 `notify_failed_total`（+ 可选 `notify_sent_total`）。
- `alert_engine/app/main.py`：通知处 try/except + 计数（+ 可选有界重试）。
- `alert_engine/app/notifiers/webhook.py`：异常带可分类 reason。
- 回归测试：失败计数 + 主链路不阻塞用例。
- PR 的 `git diff` 白名单如上；CI 双绿。

## 8. 关联

- 来源：`docs/GAP-SCAN-2026-10-13.md` **GAP-3**（MED，可观测）。
- 对照：`store.py:82` `store_errors` 已示范"失败要计数"；本需求把同一原则补到通知环节。
