# 订单 35 — UI 第四轮：告警「抑制」呈现（全栈贯通，🟢）

## 定位

你是开发工程师，证据优先。**第一动作是查清后端契约现状**：抑制在 REST 契约里有没有暴露、
数据库有没有落地字段，查完再决定动不动 backend。

## 基线

- 起点：`main`（订单 30 已落 `alert_engine/app/inhibition.py` + 可追溯记录；订单 32 已改指标命名）
- 交付分支：`feat/order-35-suppressed-ui`

## 实证（2026-10-13 取证）

- `grep -rn "抑制" frontend_dashboard/src` → **0 命中**：订单 30 的 T4（前端呈现）没有做。
- 前端已有能力可复用：`AlertLogTable`（REST 权威视图 + 行组件 memo + ack 模式）、
  `RobotDetailDrawer` 本机告警区（订单 27）、`severityColor`、主题 tokens（**严格照抄，禁止自改色**）。

## 必读

1. `alert_engine/app/inhibition.py` —— 抑制判定与记录形态（被抑制的告警留下了什么、在哪）
2. `backend_ingestion/app/alerts/`（schemas/router）与 `infra/init.sql` 的 alert 表 —— REST 契约与存储
3. `frontend_dashboard/src/api.gen.ts` / `types/robot.ts` —— 生成契约（**改契约必须走生成流程**，禁止手改 api.gen.ts）
4. `frontend_dashboard/src/components/AlertLogTable.tsx`、`RobotDetailDrawer.tsx` —— 挂载点
5. `frontend_dashboard/src/theme/tokens.ts` —— 颜色唯一来源

## 任务分解

### T1 契约核查（先做，结论进 PR）

- 回答：`GET /alerts` 现在能不能区分「已抑制」？（suppressed 字段 / 独立表 / 只有日志与指标？）
- 给出三条路之一并说明理由：
  a) 已有字段 → 前端直接接；
  b) 存储有、契约没暴露 → 补 schema 字段（走 openapi 生成流程）；
  c) 完全没有 → 后端落字段（init.sql + 写入点 + schema + 契约再生），**改动保持最小**

### T2 后端（仅当 T1 判 b/c）

- 被抑制告警在 REST 可查（列表含 `suppressed: true` 或独立 suppressed 列表接口，二选一并论证）
- 补 ≥1 个后端测试；契约快照同步（`api.gen.ts` / openapi 快照 —— contract-gate 会查）

### T3 前端呈现

- `AlertLogTable` 行内：被抑制项显示「已抑制」标记（**降视觉权重而非删除**，行不得消失）
- `RobotDetailDrawer` 本机告警区同步标记
- 若 suppressed 项默认不进未确认流，提供「显示已抑制」开关（复用既有 filter 按钮样式语言）
- 颜色全部溯源 tokens；**新增 ≥2 个前端测试**（渲染标记 / 开关行为），风格照 `RobotDetailDrawer.test.tsx`
  的受控 fetch 替身写法（**不用 msw**，happy-dom 挂起的教训在测试文件头注释里）

## 验收标准（可判定）

- [ ] `python -m pytest -q` 与 `backend_ingestion/tests` 全绿（若动了后端）
- [ ] `npm run typecheck` 0 错误；`npm test` 全绿且新增前端用例 ≥2
- [ ] `npx vite build` + `node scripts/report-bundle-size.mjs`（gzip < 320 kB）不红
- [ ] 契约变更经生成流程（contract-gate 双判定不红）
- [ ] 既有测试断言零改动、零弱化
- [ ] **产出文件白名单**：`backend_ingestion/app/alerts/**`、`alert_engine/**`、`infra/init.sql`（仅 T1=c 时）、
      `frontend_dashboard/src/**`、`frontend_dashboard/openapi.snapshot.json`（如生成流程要求）、
      `orders/35-dispatch.md`；白名单外的 diff 即无效交付

## 禁止项

1. 禁止手改 `api.gen.ts`（必须走生成流程）
2. 禁止把被抑制告警做成「不写库/不显示」的静默丢弃（订单 30 的可追溯是硬前提）
3. 禁止改 `theme/tokens.ts` 任何色值
4. 禁止整包 `import echarts` / 新增 ECharts 实例
5. 禁止弱化既有测试断言；前端测试禁止用 msw（用受控 fetch 替身）

## 交付方式

分支 `feat/order-35-suppressed-ui` → PR；描述含 T1 结论 + 前后端测试实测输出。

## 失败上报

原文粘贴错误；禁止静默失败。
