# 订单 30 — 告警抑制（inhibition）：高等级抑制同机低等级告警（🟢）

## 定位

你是开发工程师，证据优先。先读规则引擎现状再动手，禁止臆造既有实现。

## 基线

- 起点：`main`
- 交付分支：`feat/order-30-alert-inhibition`

## 依据（不能不看）

- `STATUS.md:82` C2：「告警抑制（inhibition）……离线(severity 3) 时不应同时报低电量(severity 2)」
- `external/INTEGRATION-PLAN.md` §I-5
- `alert_engine/app/rules/`（含 `offline.py` / `motor_temp` / `battery_low` 等规则）、`alert_engine/app/dedupe.py`（订单 04 去重语义）

## 必读

1. `alert_engine/app/rules/*.py` —— 各规则判定与 severity
2. `alert_engine/app/dedupe.py` —— 既有去重/幂等语义（**抑制 ≠ 去重**，不得混为一谈）
3. `alert_engine/app/store.py` —— 告警持有状态
4. `frontend_dashboard/src/components/AlertLogTable.tsx` —— 前端展示口径（订单 17）
5. `alert_engine/tests/` —— 既有测试风格

## 任务分解

### T1 抑制判定

- 同一 `robot_id` 时间窗内，存在未确认的高 severity 告警时，抑制低 severity 新告警产生
- 抑制关系是**配置表**（severity 层级或 rule 对规则），禁止写死 if-else 散落各处
- **抑制 ≠ 静默丢弃**：被抑制的告警必须留下可追溯记录（表/日志/指标），值班员能查到「这条没发出来，因为被谁压住了」

### T2 配置与可观测

- 抑制配置进 `alert_engine` config（含开关，默认开启）
- 加计数指标（被抑制条数 by reason），复用既有 metrics 命名规范（`robot_backend_*`）

### T3 测试（≥3 个真实用例）

1. 离线(3) 抑制低电量(2)：低电量不落告警 + 留下抑制记录
2. 高等级确认/清除后，低等级恢复正常产生
3. 抑制开关关闭时行为 = 现状（回归）
4. 抑制不跨机器人（A 机离线不应压制 B 机低电量）

### T4 前端（可选但建议）

- `AlertLogTable` 里被抑制项若进入 REST 视图，以「已抑制」标记呈现（**降低视觉权重而非删除**，禁止行消失）

## 验收标准（可判定）

- [ ] `python -m pytest -q` 全绿（含 ≥3 个新增抑制用例，全部真实 `def test_`）
- [ ] 抑制逻辑与去重逻辑职责分明（评审可读性：rule 层 vs store 层）
- [ ] CI 全绿（pytest / npm-test / import smoke / compileall / test 健全性）
- [ ] `orders/30-dispatch.md` 一并提交进分支

## 禁止项

1. 禁止把抑制实现成「删库/不写日志」的静默丢失（可追溯是硬要求）
2. 禁止修改既有规则的 severity 定义去"顺便解决"冲突
3. 禁止改 `pytest.ini` testpaths
4. 禁止弱化既有测试断言
5. 禁止引入新 PIP 依赖

## 交付方式

分支 `feat/order-30-alert-inhibition` → PR，描述列逐个用例输出 + 抑制配置表内容。

## 失败上报

原文粘贴错误；禁止静默失败。
