# 订单 05 — 前端监控大屏（UI 最易出缺陷，禁止自由发挥）

> 派工：`@flow.237/RobotMonitor(前端看板工程师)`
> 顺序：**第 5 单**（依赖 03 后端的 WS 与 REST）
> 依据：`spec/03`、`spec/00` §5.2 / §5.3 / §5.5、`spec/05` 阶段五

## 交付物
```
frontend_dashboard/
├── src/
│   ├── components/{FleetOverviewCards,HealthSummaryHeader,RobotDetailDrawer,JointHeatmap,AlertLogTable}.tsx
│   ├── hooks/useRobotWebSocket.ts
│   ├── store/robotStore.ts
│   ├── types/robot.ts
│   ├── theme/tokens.ts
│   ├── App.tsx  main.tsx
├── package.json  tailwind.config.js  postcss.config.js  vite.config.ts  index.html
└── README.md
```
技术栈锁定：React 18 + Vite + TS + Tailwind + ECharts(`echarts`+`echarts-for-react`) + Zustand。
**原生 WebSocket，禁止引入额外 WS 库。**

## 核心要求
1. **`useRobotWebSocket.ts`（防卡顿核心）**
   - `useRef<Map<string, RobotTelemetry>>` 缓冲，`setInterval(...,100)` 批量刷 Zustand；
     **绝不每条 WS 消息 setState**。代码注释说明为何这样防 1000 msg/s 卡顿。
   - 按 **`type` 字段分发**：`snapshot` 全量填充 / `telemetry` 增量 / `alert` prepend 到告警流。
   - 自动重连：onclose 后指数退避 1s→2s→…→10s，连接状态点变红。
   - 地址取 `import.meta.env.VITE_WS_HOST`（默认 localhost），REST 取 `VITE_API_BASE`。
2. **`FleetOverviewCards.tsx`**：`grid grid-cols-5 md:grid-cols-10 gap-2`，100 张卡，`aspect-[4/3]`。
   卡片自上而下：状态色条(h-1) → Robot ID(13px/600) → 电池条(宽=电量%，绿>50/黄15~50/红<15)
   → 最高关节温度(16px/700，0~50 绿/50~70 黄/>70 红) → 状态文字。点击打开 Drawer。
3. **`HealthSummaryHeader.tsx`**：三 KPI（在线率 = 在线数/100、当前告警数 severity≥2 未 ack、平均电量），
   `tabular-nums` 右对齐；右侧 WS 连接状态点。
4. **`RobotDetailDrawer.tsx`**：打开时用
   `GET /robots/{id}/history?minutes=5&resolution=10s` 拉历史；画选中关节的扭矩/温度/角度 ECharts 折线。
5. **`JointHeatmap.tsx`**：`grid grid-cols-6 gap-1`，每块 `h-10 rounded`，显示关节短名+温度；
   温区 `<50 绿 / 50~70 黄 / >70 红`；**按 spec/00 §5.5 的 24 个关节键名渲染**。
6. **`AlertLogTable.tsx`**：固定高 200px，`overflow-y-auto`，新告警置顶；
   每行 时间(HH:MM:SS) | 机器人 ID | 规则 chip | 消息；点击行打开对应 Drawer。

## 设计 token（spec/03 §3，严格照抄，禁止自改色）
```
背景 #0B0E14 / 卡片 #121722 / 边框·hover #1A2230
文字 #E6EDF3（主） #8B98A9（次）
状态 正常#2ECC71 预警#F1C40F 故障#FF4D4F 离线#5A6472
强调 #3B82F6
字号 卡片ID 13px/600、指标 11px/400、标题 18px/700；圆角 卡片 10px、Drawer 0
```
布局：`h-screen flex flex-col overflow-hidden`，禁止页面级滚动条（仅卡片区内部滚动）；
Drawer 从右滑出覆盖 480px，不挤压主区。

## 验收标准
- [ ] `npm install && npm run build` 通过（**RC=0**）
- [ ] 100ms 节流实现且注释清晰（绝不每消息重渲染）
- [ ] WS 按 `type` 三分支分发（snapshot/telemetry/alert）
- [ ] 100 张卡片渲染；顶栏三 KPI；底部告警流
- [ ] Drawer 打开后 ECharts 折线有数据（证明 REST history 打通）
- [ ] JointHeatmap 使用 §5.5 的 24 个关节键名
- [ ] 配色严格等于 token（不得自改）
- [ ] 无占位符；PR 含 `路径:行号`

## 禁止
- 禁止每条 WS 消息触发 React 重渲染。
- 禁止自创配色。
- 禁止引入 socket.io 等额外 WS 库。
- 禁止用 `WS_HOST` 环境变量（Vite 只读 `VITE_` 前缀，须用 `VITE_WS_HOST`）。
