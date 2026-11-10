# frontend_dashboard — 100-robot humanoid monitoring wall

Implemented per `spec/03_frontend_dashboard_and_websocket_stream.md`, `spec/00` §5.2/§5.3/§5.5, and `orders/05-frontend.md`.

## Tech stack (pinned)

React 18 + Vite 5 + TypeScript 5 + Tailwind 3 + ECharts (`echarts` + `echarts-for-react`) + Zustand 4. **Native WebSocket, no extra WS library like socket.io.**

## Quick start

```bash
cd frontend_dashboard
cp .env.example .env      # modify WS/REST addresses as needed
npm install
npm run dev               # http://localhost:5173
npm run build             # tsc --noEmit + vite build, RC=0
npm run preview           # preview the build output
```

## Bundle size (Order 09: on-demand ECharts imports)

The default entry `echarts-for-react` internally does `require("echarts")` (the whole package, cannot be tree-shaken). Order 09 switched to **core entry + on-demand registration**, with the registration list centralized in `src/lib/echartsCore.ts` (LineChart / BarChart / GaugeChart + Grid/Tooltip/Legend + CanvasRenderer).
This repo **does not use** HeatmapChart — `JointHeatmap.tsx` is pure CSS grid drawn by hand (no ECharts).

> Note: `BarChart` / `GaugeChart` are **reserved registrations**, not charts actually rendered by the current wall — the only place in the whole repo that mounts ECharts is `RobotDetailDrawer.tsx`, and only `type: 'line'`. The measured cost of both is just **+9.46 kB gzip**; Issue #62 explicitly requires keeping the line/bar/gauge trio.

`npx vite build` measured (main JS chunk `dist/assets/index-*.js`):

| Version | Main JS chunk | gzip |
| :--- | ---: | ---: |
| Full import (before Order 09) | 1,209.34 kB | 401.05 kB |
| Order 09 (LineChart only) | 675.19 kB | 226.16 kB |
| This round (+ BarChart/GaugeChart) | 703.50 kB | 235.62 kB |

> The remaining size is the **no-longer-trimmable** part of the ECharts engine (the echarts subtree after tree-shaking + the React/business baseline). See the issue's measured notes for details.

When adding/importing chart capabilities, **only** add to `echarts.use([...])` in `src/lib/echartsCore.ts`; do not import components from the `'echarts-for-react'` default entry again (otherwise the whole package re-enters the bundle).

The backend `backend_ingestion` must be started first (`ws://localhost:8000/ws/robots` and REST `/robots`).

## Environment variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `VITE_WS_HOST` | `localhost` | WS host, can be `ws://host:port` |
| `VITE_WS_PORT` | `8000` | WS port |
| `VITE_API_BASE` | `http://localhost:8000` | REST base URL |

> Vite only injects `VITE_`-prefixed variables, so the backend `.env`'s `WS_HOST` **cannot** be used.

## Directory

```
src/
├── components/
│   ├── FleetOverviewCards.tsx   # 100-card grid (grid-cols-5 md:grid-cols-10)
│   ├── HealthSummaryHeader.tsx  # top-bar 3 KPIs + WS status dot
│   ├── RobotDetailDrawer.tsx    # per-robot joint-diagnosis Drawer (480px)
│   ├── JointHeatmap.tsx         # 24-joint temperature color-block array
│   └── AlertLogTable.tsx        # bottom real-time alert stream (200px)
├── hooks/useRobotWebSocket.ts   # WS connection + 100ms throttle + exponential-backoff reconnect
├── store/robotStore.ts          # Zustand state tree
├── types/robot.ts               # types + 24-joint key names (spec/00 §5.5)
├── theme/tokens.ts              # design tokens (spec/03 §3)
├── App.tsx  main.tsx  index.css
```

## Key design

### 1. 100ms throttle (prevent 1000 msg/s jank)

100 robots × 10Hz = **1000 msg/s**. If `onmessage` does `setState` directly, React receives 1000 subscription notifications per second and the main thread is saturated. Implementation: `src/hooks/useRobotWebSocket.ts:47`:

- `onmessage` only writes to `useRef<Map<string, RobotTelemetry>>` (`src/hooks/useRobotWebSocket.ts:75`); an older frame for the same robot is naturally deduplicated when the Map overwrites it;
- an independent `setInterval(100ms)` flushes Zustand once per frame in batch (`src/hooks/useRobotWebSocket.ts:52`).

Effect: no matter how many messages arrive, React is notified at most 10 times per second. Measured: 1200 messages triggered only **11 renders**.

### 2. WS dispatches on `type` into three branches (spec/00 §5.2)

`snapshot` fills everything / `telemetry` writes ref incrementally / `alert` prepends to the alert stream. See `src/hooks/useRobotWebSocket.ts:95`.

### 3. Auto-reconnect (exponential backoff 1s→2s→4s→8s→10s cap)

See `src/hooks/useRobotWebSocket.ts:143`. After connecting it must **stay alive stably for 5s** before the backoff resets (`RECONNECT_STABLE_MS`, `src/hooks/useRobotWebSocket.ts:27`), otherwise server-side jitter would keep the backoff stuck at 1s forever.

### 4. Offline placeholders (the 100 cards must not be blank)

The store's initial value already includes 100 offline placeholders (`src/store/robotStore.ts:65`), so the first screen shows 100 cards even before the WS connects.

### 5. Incremental alert-stream rendering (fleeting alerts shouldn't drive a full-table re-layout)

`AlertLogTable`'s rows are split into `memo(AlertRow)`: when a new alert arrives only **the newly added row** mounts; existing rows whose props are unchanged → memo hits → are not rebuilt.

The key prerequisite is that `key` can only use the alert's own stable fields (`ts-robot_id-rule_id`), **not `idx`** — under prepend semantics an idx shift moves all keys, invalidating all memos. Measured (`node .e2e/run_alert_rows.mjs`): pushing 5 new alerts

| Implementation | New DOM row nodes |
| :--- | ---: |
| key with idx | 215 |
| Stable key + memo (current) | **5** |

`ALERT_RENDER_LIMIT = 60` controls how many rows the table actually renders, distinct from the store's `ALERT_BUFFER_LIMIT = 200` (for "cumulative" counting).

### 6. Temperature/battery color scale

`<50` green / `50~70` yellow / `>70` red (`src/theme/tokens.ts:47`); battery `>50` green / `15~50` yellow / `<15` red (`src/theme/tokens.ts:55`).

## Local verification

```bash
npm run build                      # RC=0
node .e2e/run.mjs                  # WS flood + three branches + prepend (real hook/store + fake WS server)
node .e2e/run_reconnect.mjs        # exponential-backoff reconnect
node .e2e/run_drawer.mjs           # REST history → ECharts (fake backend)
node .e2e/run_alert_rows.mjs       # alert row incremental render (memo works, no full-table rebuild)
node .e2e/run_offline.mjs          # 100 offline placeholder cards when no data (need `npm run preview` first)
```

One-click full run (auto-starts the preview server):

```bash
bash .e2e/run_all.sh               # 6/6
```
