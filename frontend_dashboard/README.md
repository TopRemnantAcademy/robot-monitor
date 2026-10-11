# frontend_dashboard — 100 台人形机器人监控大屏

依据 `spec/03_frontend_dashboard_and_websocket_stream.md`、`spec/00` §5.2/§5.3/§5.5、
`orders/05-frontend.md` 实现。

## 技术栈（锁定）

React 18 + Vite 5 + TypeScript 5 + Tailwind 3 + ECharts（`echarts` + `echarts-for-react`）
+ Zustand 4。**原生 WebSocket，无 socket.io 等额外 WS 库。**

## 快速开始

```bash
cd frontend_dashboard
cp .env.example .env      # 按需修改 WS/REST 地址
npm install
npm run dev               # http://localhost:5173
npm run build             # tsc --noEmit + vite build，RC=0
npm run preview           # 预览构建产物
```

## Bundle 体积（订单 09：ECharts 按需引入）

ECharts 默认入口 `echarts-for-react` 内部 `require("echarts")`（整包，无法 tree-shake）。
订单 09 改为 **core 入口 + 按需注册**，注册清单集中在
`src/lib/echartsCore.ts`（LineChart / BarChart / GaugeChart + Grid/Tooltip/Legend +
CanvasRenderer）。
本仓库**不使用** HeatmapChart —— `JointHeatmap.tsx` 是纯 CSS grid 自绘（无 ECharts）。

> 说明：`BarChart` / `GaugeChart` 属 **预留注册**，不是当前大屏实际渲染的图表 ——
> 全仓挂载 ECharts 的只有 `RobotDetailDrawer.tsx`，且只有 `type: 'line'`。
> 二者代价实测仅 **+9.46 kB gzip**，Issue #62 明确要求保留 line/bar/gauge 三件套。

`npx vite build` 实测（主 JS chunk `dist/assets/index-*.js`）：

| 版本 | 主 JS chunk | gzip |
| :--- | ---: | ---: |
| 全量引入（订单 09 前） | 1,209.34 kB | 401.05 kB |
| 订单 09（仅 LineChart） | 675.19 kB | 226.16 kB |
| 本次（+ BarChart/GaugeChart） | 703.50 kB | 235.62 kB |

> 剩余体积为 ECharts 引擎的**不可再裁**部分（tree-shake 后 echarts 子树 + React/业务基线）。
> 详见 issue 的实测说明。

新增/导入图表能力时，请**只**在 `src/lib/echartsCore.ts` 里 `echarts.use([...])` 增补，
不要再从 `'echarts-for-react'` 默认入口导入组件（否则整包会重新进 bundle）。

后端需先启动 `backend_ingestion`（`ws://localhost:8000/ws/robots` 与 REST `/robots`）。

## 环境变量

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `VITE_WS_HOST` | `localhost` | WS 主机，可写 `ws://host:port` |
| `VITE_WS_PORT` | `8000` | WS 端口 |
| `VITE_API_BASE` | `http://localhost:8000` | REST 基址 |

> Vite 只注入 `VITE_` 前缀变量，因此**不能**使用后端 `.env` 里的 `WS_HOST`。

## 目录

```
src/
├── components/
│   ├── FleetOverviewCards.tsx   # 100 卡网格（grid-cols-5 md:grid-cols-10）
│   ├── HealthSummaryHeader.tsx  # 顶栏三 KPI + WS 状态点
│   ├── RobotDetailDrawer.tsx    # 单机关节诊断 Drawer（480px）
│   ├── JointHeatmap.tsx         # 24 关节温度色块阵列
│   └── AlertLogTable.tsx        # 底部实时告警流（200px）
├── hooks/useRobotWebSocket.ts   # WS 接入 + 100ms 节流 + 指数退避重连
├── store/robotStore.ts          # Zustand 状态树
├── types/robot.ts               # 类型 + 24 关节键名（spec/00 §5.5）
├── theme/tokens.ts              # 设计 token（spec/03 §3）
├── App.tsx  main.tsx  index.css
```

## 关键设计

### 1. 100ms 节流（防 1000 msg/s 卡顿）

100 台 × 10Hz = **1000 msg/s**。若 `onmessage` 里直接 `setState`，React 每秒收到 1000 次
订阅通知，主线程被打满。实现见 `src/hooks/useRobotWebSocket.ts:47`：

- `onmessage` 只写入 `useRef<Map<string, RobotTelemetry>>`（`src/hooks/useRobotWebSocket.ts:75`），
  同台机器人的旧帧被 Map 覆盖自然去重；
- 独立 `setInterval(100ms)` 每帧批量刷一次 Zustand（`src/hooks/useRobotWebSocket.ts:52`）。

效果：无论进来多少消息，React 每秒最多 10 次通知。实测 1200 条消息仅触发 **11 次渲染**。

### 2. WS 按 `type` 三分支分发（spec/00 §5.2）

`snapshot` 全量填充 / `telemetry` 写 ref 增量 / `alert` prepend 告警流。
见 `src/hooks/useRobotWebSocket.ts:95`。

### 3. 自动重连（指数退避 1s→2s→4s→8s→10s 封顶）

见 `src/hooks/useRobotWebSocket.ts:143`。连上后需**稳定存活 5s** 才重置退避
（`RECONNECT_STABLE_MS`，`src/hooks/useRobotWebSocket.ts:27`），否则服务端抖动时退避永远停在 1s。

### 4. 离线占位（100 台不得空白）

store 初值即 100 台离线占位（`src/store/robotStore.ts:65`），WS 未连上首屏也有 100 张卡。

### 5. 告警流增量渲染（无常的告警不该驱动整表重排）

`AlertLogTable` 的行拆为 `memo(AlertRow)`：新告警进来只有**新增那一行**挂载，
已有行 props 未变 → memo 命中 → 不重建。

关键前提是 `key` 只能用告警自身的稳定字段（`ts-robot_id-rule_id`），
**不能带 `idx`** —— prepend 语义下 idx 整体平移会让所有 key 变化，memo 全部失效。
实测（`node .e2e/run_alert_rows.mjs`）：push 5 条新告警

| 实现 | 新增 DOM 行节点数 |
| :--- | ---: |
| 带 idx 的 key | 215 |
| 稳定 key + memo（当前） | **5** |

`ALERT_RENDER_LIMIT = 60` 控制表格实际渲染条数，与 store 的
`ALERT_BUFFER_LIMIT = 200`（供「累计」计数）职责不同。

### 6. 温度/电量色阶

`<50` 绿 / `50~70` 黄 / `>70` 红（`src/theme/tokens.ts:47`）；电量 `>50` 绿 / `15~50` 黄 / `<15` 红
（`src/theme/tokens.ts:55`）。

## 本地验证

```bash
npm run build                      # RC=0
node .e2e/run.mjs                  # WS 洪流 + 三分支 + prepend（真实 hook/store + 假 WS 服务端）
node .e2e/run_reconnect.mjs        # 指数退避重连
node .e2e/run_drawer.mjs           # REST history → ECharts（假后端）
node .e2e/run_alert_rows.mjs       # 告警行增量渲染（memo 生效，不整表重建）
node .e2e/run_offline.mjs          # 无数据时 100 张离线占位卡（需先 `npm run preview`）
```

一键全跑（含自动起 preview 服务）：

```bash
bash .e2e/run_all.sh               # 6/6
```
