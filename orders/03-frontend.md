# [03] 前端监控大屏（React + Vite + Tailwind + ECharts）

## 目标
实现 `frontend_dashboard/`：接收后端 `ws://{server}:8000/ws/robots` 推送的 100 台机器人 10Hz 状态，呈现「百机卡片墙 + 全局指标看板 + 单机关节热力诊断 Drawer + 实时告警流」。**UI 是本项目最易出缺陷处，必须严格按规范，禁止自由发挥。**

## 依据规范
- `spec/00_system_architecture_spec.md`（WebSocket 消息格式）
- `spec/03_frontend_dashboard_and_websocket_stream.md`（目录 / 设计 token / 组件规范 / UI 细节）

## 硬性要求（交给开发 NPC 的指令）
1. `useRobotWebSocket.ts` 必须实现 100ms 节流（`useRef` 缓冲 + `setInterval` 批量刷 Zustand）与自动重连（指数退避），注释说明为何这样防止 1000 msg/s 卡顿。
2. `FleetOverviewCards.tsx` 按 §5.1 实现 100 卡网格，`grid grid-cols-5 md:grid-cols-10`，严格使用 §3 暗色 token（背景 #0B0E14 / 卡片 #121722 / 状态色 绿#2ECC71 黄#F1C40F 红#FF4D4F 灰#5A6472）。
3. `RobotDetailDrawer.tsx` + `JointHeatmap.tsx` 实现关节温度色块阵列（温区 `<50` 绿 / `50~70` 黄 / `>70` 红）。
4. `HealthSummaryHeader.tsx` 实现三 KPI（在线率 / 告警数 / 平均电量）+ WS 连接状态点。
5. `AlertLogTable.tsx` 订阅告警流并置顶新告警。

## 验收标准
- [ ] 无占位符；`npm install && npm run build` 通过（RC=0）
- [ ] 前端 100ms 节流实现且注释清晰（绝不每条 WS 消息直接 setState 重渲染）
- [ ] UI 严格使用 spec/03 §3 暗色 token，未自改色
- [ ] 卡片墙 / 顶栏 / Drawer / 告警流四区齐全
- [ ] 通过 PR 提交，含 `路径:行号` 实证
