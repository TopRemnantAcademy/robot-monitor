# 订单 33 — 前端大机群渲染护栏：稳态探针 + 回归测试（🟢）

## 定位

你是开发工程师，证据优先。**第一动作是自证现状**：先确认下面每条断言是否仍然成立，不成立就在 PR 里报告差异再动手，禁止照单施工。

## 基线

- 起点：`main`（含订单 27：`feat(ui)` Drawer 扭矩负载条 / 告警确认 / 历史窗口已合入）
- 交付分支：`feat/order-33-fleet-render-guard`

## 依据（自证清单）

- ISSUE #75 结构性共享：`RobotDetailDrawer` 只订阅 `s.robots.get(selectedRobotId)`，不订阅整张 Map
- Zustand store 的 WS 热路径有 100ms 节流；机群视图（`.tsx` 中的 Cards / HealthSummaryHeader / FleetHealthAnalytics）要求用 `useShallow` 桶形选择器
- `HistoryPointOut`（`api.gen.ts:292-312`）含后端聚合字段，趋势类 UI **禁止客户端重算**
- **推测（需先验证）**：目前没有测试断言「100 台卡片墙的渲染次数不随机群规模线性爆炸」

## 必读

1. `frontend_dashboard/src/store/robotStore.ts` —— 热路径与 `computeFleetSample`
2. `frontend_dashboard/src/components/FleetOverviewCards.tsx` / `HealthSummaryHeader.tsx` / `FleetHealthAnalytics.tsx`
3. `frontend_dashboard/src/components/RobotDetailDrawer.tsx` —— 订单 27 的最新形态
4. `frontend_dashboard/src/theme/tokens.ts` —— **严格照抄，禁止自改色**
5. `frontend_dashboard/src/lib/echartsCore.ts` —— 按需注册清单，禁止新增整包引入

## 任务分解

### T1 现状自证（先做）

给出一句结论：现有视图在 N=100 机器人下，单帧更新触发哪些组件重渲染？（用代码证据回答：选择器粒度 / useShallow 覆盖 / memo 边界）

### T2 渲染护栏测试（vitest）

- 新增一个测试：注入 100 台机器人的批量遥测帧后，断言**受约束组件的渲染次数在阈值内**（自己给阈值并说明理由，不接受"随便定"）
- 至少覆盖：机群卡片墙 / 健康分析条
- 禁止为了过阈值而放宽渲染判断精度（这是自欺）

### T3 修复发现的偏差（若有）

- 若 T1 发现某视图订阅了整张 Map 或缺少 `useShallow`，修之并附 T2 用例保护
- 颜色一律溯源 tokens，**零自创色相**

## 验收标准（可判定）

- [ ] `npm test` 全绿（既有用例断言零改动，新增护栏用例 ≥2）
- [ ] `npm run typecheck` 0 错误
- [ ] `npx vite build` 成功；`node scripts/report-bundle-size.mjs` gzip < 320 kB
- [ ] 零新增 ECharts 实例 / 整包引入
- [ ] PR 描述含 T1 的结论 + 阈值依据
- [ ] `orders/33-dispatch.md` 一并提交进分支

## 禁止项

1. 禁止改 `theme/tokens.ts` 任何色值
2. 禁止整包 `import echarts` 或新增模块注册
3. 禁止弱化/删改既有测试断言
4. 禁止客户端重算后端已聚合字段（battery_pct / max_joint_temp / status_code）
5. 禁止为跑快而 `vi.useFakeTimers` 掉真实数据流（除非明确只测节流）
6. 禁止动 backend（本单纯前端）

## 交付方式

分支 `feat/order-33-fleet-render-guard` → PR，描述列四道门禁实测输出。

## 失败上报

原文粘贴错误；禁止静默失败。
