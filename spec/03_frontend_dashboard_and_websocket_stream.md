# File 03: 前端轻量级监控大屏与 WebSocket 实时流规范（UI 详细版）

> 本模块接收后端 `ws://{server}:8000/ws/robots` 推送的 100 台机器人 10Hz 状态，
> 实现「百机卡片墙 + 全局指标看板 + 单机关节热力诊断 Drawer + 实时告警流」。
> **UI 是本系统最容易出缺陷的环节，本文档对视觉与交互给出确定性规范，禁止 AI 自由发挥。**

## 1. 技术栈（锁定）

- **框架**：React 18 + Vite + TypeScript（不要用 Next.js，纯 SPA 即可）。
- **样式**：Tailwind CSS（暗色工业大屏主题）。
- **图表**：ECharts（`echarts` + `echarts-for-react`）用于关节扭矩/温度折线。
- **状态**：Zustand（`robotStore.ts`）。
- **通信**：原生 WebSocket（`useRobotWebSocket.ts`），禁止引入额外 WS 库。

## 2. 工程目录结构（强制）

```
frontend_dashboard/
├── src/
│   ├── assets/
│   ├── components/
│   │   ├── FleetOverviewCards.tsx   # 100 台机器人状态卡片网格
│   │   ├── HealthSummaryHeader.tsx  # 顶部全局指标
│   │   ├── RobotDetailDrawer.tsx    # 单机关节热力诊断 Drawer
│   │   ├── AlertLogTable.tsx        # 底部实时告警日志流
│   │   └── JointHeatmap.tsx         # 关节温度色块阵列（Drawer 内）
│   ├── hooks/
│   │   └── useRobotWebSocket.ts     # WS 接入 + 100ms 节流 + 自动重连
│   ├── store/
│   │   └── robotStore.ts            # Zustand 全局状态树
│   ├── types/
│   │   └── robot.ts                 # TypeScript 类型
│   ├── theme/
│   │   └── tokens.ts                # 颜色 / 间距 token（暗色主题）
│   ├── App.tsx
│   └── main.tsx
├── package.json
├── tailwind.config.js
├── postcss.config.js
├── vite.config.ts
└── index.html
```

## 3. 设计系统（Design Tokens，暗色工业大屏）

```
背景：    #0B0E14（主背景）  #121722（卡片）  #1A2230（卡片边框/hover）
文字：    #E6EDF3（主）  #8B98A9（次要）
状态色：  正常 #2ECC71（绿）  预警 #F1C40F（黄）  故障 #FF4D4F（红）  离线 #5A6472（灰）
强调色：  #3B82F6（蓝，用于选中/链接）
字号：    卡片 ID 13px/600  指标 11px/400  标题 18px/700
圆角：    卡片 10px  Drawer 0（全屏侧滑）
```

## 4. 布局（App.tsx，三区网格）

```
┌───────────────────────────────────────────────┐
│ HealthSummaryHeader（顶栏：在线率/告警数/平均电量）│ 高 56px
├───────────────────────────────────────────────┤
│ FleetOverviewCards（100 卡片，滚动区，占满）      │ flex-1
├───────────────────────────────────────────────┤
│ AlertLogTable（底部实时告警流，固定高 200px）     │ 高 200px
└───────────────────────────────────────────────┘
```
- 整体 `h-screen flex flex-col overflow-hidden`，禁止页面级滚动条（仅卡片区内部滚动）。
- Drawer 从右侧滑出，覆盖右侧 480px，不挤压主区。

## 5. 关键组件规范

### 5.1 FleetOverviewCards.tsx（100 卡片墙）
- 容器 `grid grid-cols-5 md:grid-cols-10 gap-2`，每张卡片 `aspect-[4/3]`。
- 卡片内容（自上而下）：
  1. 顶部状态条：`h-1` 细条，颜色=状态色。
  2. Robot ID（如 `H-042`），13px/600，左对齐。
  3. 电池条：宽度=电量%，颜色 绿(>50)/黄(15~50)/红(<15)。
  4. 最高关节温度：大字 16px/700，颜色按 `0~50 绿 / 50~70 黄 / >70 红`。
  5. 状态文字：正常/预警/故障/离线（对应色）。
- 点击卡片 → 打开 RobotDetailDrawer（传 robot_id）。
- 卡片数据来自 Zustand `robots` Map，**不**在收到每条 WS 消息时重渲染（见 §6 节流）。

### 5.2 HealthSummaryHeader.tsx（顶栏）
- 三个 KPI 块：在线率（在线数/100，%）、当前告警数（severity≥2 未确认）、平均电量。
- 数字用 `tabular-nums`，右对齐；标签 11px 次要色。
- 右侧显示 WS 连接状态点（绿=已连 / 红=断开重连中）。

### 5.3 RobotDetailDrawer.tsx（单机关节诊断）
- 打开时向后端请求该机器人最近 5 分钟历史（从 `robot_telemetry` 连续聚合或原始表拉取）。
- 内容：
  1. 关节温度热力阵列 `JointHeatmap`：每个关节一个色块，颜色按温区；hover 显示关节名+温度。
  2. 选中关节的扭矩/温度/角度折线（ECharts，最近 5 分钟，10s 分辨率）。
  3. 基本信息：电量、状态、最后更新时间。

### 5.4 JointHeatmap.tsx（色块阵列）
- 网格 `grid grid-cols-6 gap-1`，每块 `h-10 rounded` 显示关节短名 + 温度值。
- 温区：`<50` 绿 / `50~70` 黄 / `>70` 红；与卡片色阶一致。
- 数据来源：Drawer 打开时订阅该机器人增量 + 历史快照。

### 5.5 AlertLogTable.tsx（底部告警流）
- 固定高 200px，`overflow-y-auto`，新告警置顶（prepend）。
- 每行：时间（HH:MM:SS）| 机器人 ID | 规则标签（彩色 chip）| 消息。
- 点击行 → 打开对应机器人 Drawer。
- 数据源：订阅 Redis `robot:alerts`（经后端 WS 转发）或独立告警 WS。

## 6. 高性能 WebSocket 接入（useRobotWebSocket.ts）—— 防卡顿核心

```ts
// 维护 ref 保存 100 台最新状态字典，绝不每消息 setState
const latestDataRef = useRef<Map<string, RobotTelemetry>>(new Map());

// 100ms (10Hz) 统一刷入 Zustand，避免 1000 msg/s 直接触发 React 重渲染
useEffect(() => {
  const timer = setInterval(() => {
    updateStoreBatch(Array.from(latestDataRef.current.values()));
  }, 100);
  return () => clearInterval(timer);
}, []);

// 自动重连：onclose 后指数退避（1s→2s→...→10s）重连，连接点变红
```

- 连接 `ws://{server}:8000/ws/robots`，首次收到 100 台快照后填充 store。
- `server` 从 `import.meta.env.VITE_WS_HOST` 读取，默认 `localhost`。

## 7. 类型定义（robot.ts）

```ts
export type RobotStatus = 0 | 1 | 2 | 3; // 离线/正常/预警/故障
export interface RobotTelemetry {
  robot_id: string;
  status_code: RobotStatus;
  battery_pct: number;
  max_joint_temp: number;
  joint_temperatures: Record<string, number>;
  joint_torques: Record<string, number>;
  joint_positions: Record<string, number>;
  error_code: number;
  ts: number;
}
export interface RobotAlert {
  robot_id: string; rule_id: string; severity: number; message: string; ts: number;
}
```

## 8. AI 编写指令（交给 NPC 的 prompt）

> "请严格根据 `spec/03_frontend_dashboard_and_websocket_stream.md` 生成 TypeScript + React (Vite + Tailwind) 前端。
> 硬性要求：
> 1. useRobotWebSocket.ts 必须实现 100ms 节流（useRef 缓冲 + setInterval 批量刷 store）与自动重连，注释说明为何这样防止 1000 msg/s 卡顿。
> 2. FleetOverviewCards.tsx 按 §5.1 实现 100 卡网格，严格使用 §3 的暗色 token，禁止自行改色。
> 3. RobotDetailDrawer.tsx + JointHeatmap.tsx 实现关节温度色块阵列（温区按 §5.4）。
> 4. HealthSummaryHeader.tsx 实现三个 KPI + WS 连接状态点。
> 5. AlertLogTable.tsx 订阅告警流并置顶新告警。
> 6. 不使用占位符；提供 package.json / tailwind.config.js / vite.config.ts / index.html / README。"
