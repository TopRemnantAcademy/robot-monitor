# 派工单：订单 17 — 告警闭环（ack / escalation / silence）

> 依据：`docs/design/ENTERPRISE-GAP-ANALYSIS.md` §1（H1/H2）与 §2.3（缺口 E4）
> 触发方式：`post-issue-comment`（本文件不含触发词）

---

## 0. 现状：Schema 已埋一半，能力没接（最危险的"半成品"）

| 事实 | 证据（file:line） |
|---|---|
| `robot_alert.ack` 列已建，默认 FALSE | `infra/init.sql:62` |
| 专门为未确认告警建了部分索引 | `infra/init.sql:92`（`idx_alert_ack ... WHERE ack = FALSE`） |
| **全仓无任何代码读写 `ack`** | `grep -rn "ack" backend_ingestion/app/ alert_engine/app/ frontend_dashboard/src/` → 无业务命中 |
| severity 只有 2/3 级常量，**无升级** | `alert_engine/app/rules/*.py` |
| 无静默机制 | 无对应代码 |

> 后果：值班员看到告警**无法认领、无法关闭、无法停嘴**。
> 这套东西在演示时能过，交付到车间第一天就会被投诉。

---

## 1. 任务

### T1 — 告警确认（ack）
- 后端新增（**需 `admin` 角色，见订单 11 的 RBAC**）：
  - `POST /alerts/{alert_id}/ack`：置 `ack=TRUE`，记 `ack_by`（从 JWT `sub` 取）、`ack_at`
  - `POST /alerts/{alert_id}/unack`：撤回
  - `GET /alerts?ack=false&limit=50&offset=0`：**分页**查询（解 H5）
- `infra/init.sql` 需补列 `ack_by VARCHAR(64)`、`ack_at TIMESTAMPTZ`，
  **同时**更新 `robot_alert` 对应的 ORM 模型（AGENTS.md §4 要求两处同步）
- ⚠️ **已核对的硬事实**：`robot_alert` **没有主键、也没有 ID 列**
  （`infra/init.sql:55-64`，列只有 `time/robot_id/rule_id/severity/message/detail/ack`；
  对比 `robot_telemetry` 有 `PRIMARY KEY (time, robot_id)`，见 `init.sql:27`）。
  因此**无法用 ID 寻址单条告警** —— 必须先加 ID 列（推荐 `BIGSERIAL`，
  或 `(time, robot_id, rule_id)` 复合定位）并写清对已部署库的迁移方式。
  这是本单的**第一个 blocker**，不得跳过。

### T2 — 升级（escalation）
对齐 Alertmanager 的 `repeat_interval` 语义（源：`external/alertmanager`）：
- 新增 `alert_engine/app/escalation.py`
- 规则：`severity=3` 且 **5 分钟未 ack** → 标记 `escalated=TRUE`，再次 `PUBLISH robot:alerts`（`type:"alert"` 信封不变）
- 阈值进 `alert_engine/app/config.py`：`escalate_after_s: int = 300`
- **禁止**改变已有 4 条规则的判定逻辑

### T3 — 静默（silence）
对齐 Alertmanager `silence`：
- Redis key `alert:silence:{robot_id}`（或 `alert:silence:all`），带 TTL
- `emit()` 前置检查（位置：`alert_engine/app/main.py:53`，与订单 10 的 inhibition **同一入口，注意顺序**）
- 顺序必须是：**silence → inhibition → dedup**，并在代码注释里写清这个顺序理由

### T4 — 前端接应
- `AlertLogTable` 每行加「确认」按钮（仅 `admin` 可见）
- 已确认告警：视觉降噪（灰度 + ✓ 标记），**不得隐藏**（可追溯）
- 升级告警：显著色 + `⚠ 已升级` 标记
- 新增筛选：`全部 / 未确认 / 已升级`
- 新增分页（对接 `GET /alerts`）

---

## 2. 验收标准

- [ ] `POST /alerts/{id}/ack` 后，DB 中 `ack=TRUE` 且 `ack_by`/`ack_at` 有值（**贴真实 SQL 查询结果**）
- [ ] 重复 ack **幂等**（不报错、不重复写）
- [ ] `GET /alerts?ack=false` 返回分页结果，`offset` 生效
- [ ] `severity=3` 未确认 5 分钟 → `escalated` 变 TRUE 且再次发布
- [ ] 静默期内告警不落库、不发布；TTL 到期自动恢复
- [ ] **顺序验证**：静默生效时，inhibition 与 dedup 都不会被触发（贴日志）
- [ ] `infra/init.sql` 与 ORM 模型两处同步（PR 描述给出两处 file:line）
- [ ] `tsc --noEmit` 0 错误；`vite build` RC=0
- [ ] **实际运行测试**，不是只看代码

## 3. 禁止项

1. **禁止**改 4 条规则自身的判定逻辑（只加闭环层）
2. **禁止**由前端传入 `ack_by`（必须从 JWT 取，否则可伪造）
3. **禁止**改 WS `type:"alert"` 信封结构
4. **禁止**直接改 `infra/init.sql` 而不写清对已部署库的迁移方式
5. **禁止**让已确认告警从前端消失（必须是降噪而非隐藏）
6. **禁止**伪造测试结果
7. **禁止**只推分支就宣称完成 —— 尝试 push + 开 PR；失败则**原文报告**

## 4. 冲突提示

- 与订单 10（已在进行）同改 `alert_engine/app/main.py:53` 的 `emit()`。
  **本单以此为准**：`emit()` 入口顺序 = silence → inhibition → dedup。
  合并时订单 10 的 `is False` 判定（不可用 falsy）必须保留。
- 与订单 11 共享 RBAC：`admin` 角色名必须一致。
