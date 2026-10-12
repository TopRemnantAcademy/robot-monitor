# 订单 27 — UI 强化第三轮：Drawer 扭矩负载条 + 告警确认按钮 + 历史窗口切换（🟢）

> 执行方式变更记录：原计划派发 CNB NPC @codebuddy 执行；因平台 AI 积分耗尽（402），
> 改由本地直接实现后走 PR + CI 门禁合并。本文件作为订单规格存档随交付分支入库。

## 定位

你是开发工程师，证据优先。所有断言基于磁盘实存代码（file:line 可查），禁止臆测。

## 基线

- 分支起点：`main @ c7719f5`（订单 26 合并后的 Drawer 单机诊断深化版）
- 交付分支：`feat/order-27-ui-drawer-round3`

## 必读文件（实现前逐个读，禁止跳过）

1. `frontend_dashboard/src/components/RobotDetailDrawer.tsx` —— 本单全部改动的主战场
2. `frontend_dashboard/src/components/AlertLogTable.tsx` —— 确认按钮既有模式（canAck/busyId/撤回|确认 文案）
3. `frontend_dashboard/src/auth/authStore.ts` —— `useIsAdmin()`（UI 裁剪，非安全边界）
4. `frontend_dashboard/src/auth/alertsApi.ts` —— `ackAlert/unackAlert`；403/404 文案；POST 空 body 契约
5. `frontend_dashboard/src/api/robotHistory.ts` —— `HISTORY_MINUTES/HISTORY_RESOLUTION/robotHistoryKey/fetchRobotHistory`
6. `frontend_dashboard/src/api.gen.ts:292-312` —— `HistoryPointOut` 含 `battery_pct/max_joint_temp/status_code`
7. `frontend_dashboard/src/components/__tests__/RobotDetailDrawer.test.tsx` —— 既有 9 用例是**契约**，断言一个都不许改
8. `backend` `main.py` `/history` 路由 —— `minutes: int = 5` **无上限约束**（`span_s = max(1, minutes) * 60`），加窗口无需后端改动
9. `backend` `alerts` `router.py:36-37` —— `limit: Query(ge=1, le=_MAX_LIMIT)`，`_MAX_LIMIT = 500`（Drawer 拉 200 条在其内）
10. `frontend_dashboard/src/theme/tokens.ts` —— 严格照抄，禁止自改色
11. `frontend_dashboard/src/lib/echartsCore.ts` —— 按需注册清单；**禁止新增 ECharts 实例/模块注册**

## 任务分解

### T1 关节扭矩负载条（Drawer，纯 CSS）

- 24 关节逐行条形：名称 + 条 + 数值；2 列网格
- 满刻度 = 当前帧有值关节的最大 |扭矩|；全零/缺失时钳到 1 防除零（条宽 0 = 无负载语义）
- 条宽 = `|v| / maxAbs * 100`，色 `COLORS.accent`；轨道 `COLORS.border`
- 点击切换 `selectedJoint`（与热力图联动）
- **aria-label 必须为 `扭矩 <joint> <v.toFixed(2)>N·m`**（无数据时 `扭矩 <joint> 无数据`）——
  热力图关节块的 aria-label 以关节名开头，既有用例 `findByRole('button', { name: /^left_elbow_pitch/ })`
  依赖该前缀唯一性，扭矩条前缀「扭矩 」保证两者在可访问性树互不混淆
- **零新增 ECharts 实例**（包体积门禁）

### T2 告警确认/撤回按钮（Drawer 告警区）

- `useIsAdmin()` 门控显隐（403 由后端兜底，前端只是"不让看见点不动的东西"）
- busy 态按 `alert_id` 钳制（复用 AlertLogTable 的 busyId 模式），按钮文案 `确认`/`撤回`
- 成功后 `queryClient.invalidateQueries({ queryKey: ['robot-alerts', selectedRobotId] })`
  —— REST 是 ack 状态权威视图，重拉即对账
- **错误文案不得含 "HTTP 5"**：500 用例里 `getByText(/HTTP 500/)` 必须唯一命中历史图错误。
  含 5xx 的原始 message 归一为 `告警确认操作失败`；403/404/401 文案照常透传（有操作价值）

### T3 历史窗口切换（Drawer 折线/电量区）

- `HISTORY_WINDOW_OPTIONS = [5, 30, 60]`（robotHistory.ts 导出）
- `fetchRobotHistory(robotId, signal?, minutes = HISTORY_MINUTES)`、
  `robotHistoryKey(robotId, minutes = HISTORY_MINUTES)` → `['robot-history', robotId, minutes]`
  —— minutes 进 key，切窗口 = 换 key = react-query 自动重拉，不写 effect
- 窗口按钮 `5m/30m/60m`，激活态 accent 底/边框（同 AlertLogTable 筛选钮样式语言）
- 标题文案 `最近 {historyMinutes} 分钟`（关节图头 + 电量趋势头 + 空态三处同步）
- `resolution` 恒为 `10s` 不随窗口变

## 验收标准（可判定）

- [ ] `npm run typecheck` 0 错误
- [ ] `npm test` 全绿；既有 9 用例断言零改动；新增 3 用例（见测试约束）
- [ ] `npx vite build` 成功；`node scripts/report-bundle-size.mjs` gzip < 320 kB（基线 245.68 kB）
- [ ] 零新增 ECharts 实例/模块注册
- [ ] 全部颜色可溯源 `theme/tokens.ts`，零自创色相

## 测试约束（3 个新用例，置于既有 9 用例之后）

- **A 窗口切换**：fetch 替身记录 URL → 点击 `30m` → 断言出现 `minutes=30`（且 `resolution=10s`）请求；
  标题断言用**全串精确匹配**（`最近 30 分钟` 子串会同时命中关节图头与电量趋势头 → 多元素错误）
- **B 扭矩条**：`findByRole('button', { name: '扭矩 left_elbow_pitch 1.70N·m' })`（实时帧样例 1.7）→
  点击 → `readChartOption()` 扭矩 series 变为 `[1.5, 1.7]`
- **C 告警确认**（置于 describe 末尾 + 结束前还原 authStore）：
  先以默认非 admin 断言无 `确认` 按钮 → `useAuthStore.setState({ status:'authenticated', email:'a@b.c', role:'admin' })`
  → 有状态 fetch 替身（POST `/alerts/11/ack` 翻转后续 GET 的 `ack:true`，响应含 AckResult 全字段含 `changed:true`）
  → 点击 → 断言 `已确认` 出现

## 禁止项

1. 禁止整包 `import echarts` / 新增模块注册（订单 09 红线）
2. 禁止改 `theme/tokens.ts` 任何色值
3. 禁止动 backend（本单纯前端）
4. 禁止弱化/删改既有测试断言
5. 禁止动 orders/ 其余文件与 spec/

## 交付方式

1. 分支 `feat/order-27-ui-drawer-round3`（自 `main @ c7719f5`）
2. 把本单文件以 `orders/27-dispatch.md` 一并提交进分支
3. PR：标题 `feat(ui): Drawer 扭矩负载条 + 告警确认 + 历史窗口切换`，
   描述列四道门禁实测输出（typecheck / vitest 计数 / build / bundle gzip）

## 失败上报

门禁失败/配额 402 时原文粘贴错误输出，禁止静默失败或"应该没问题"式收尾。
