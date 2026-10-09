# .e2e — 前端集成验证（不参与生产构建）

用真实产品代码（`src/hooks/useRobotWebSocket.ts`、`src/store/robotStore.ts`、
`src/components/*`）跑在 jsdom / headless Chromium 上，验证 spec/03 的硬性契约。

## 运行

```bash
cd frontend_dashboard
npm install
bash .e2e/run_all.sh        # 一键全跑（5 项）
```

单独跑：

```bash
E2E_WS_PORT=8123 node .e2e/build.mjs   # 先把 src 打成 Node CJS
node .e2e/run.mjs                       # WS 洪流 + type 三分支 + 100ms 节流
node .e2e/run_reconnect.mjs             # 指数退避 1s→2s→4s→8s
E2E_API_BASE=http://localhost:8201 node .e2e/build.mjs
node .e2e/run_drawer.mjs                # REST history → ECharts
node .e2e/run_offline.mjs               # 无数据 → 100 张离线占位卡（需 vite preview 在 4173）
node .e2e/screenshot.mjs                # 真实浏览器截图（含假后端）
```

## 说明

- `entry.tsx` / `entry_drawer.tsx`：把产品组件挂到 jsdom 的最小入口。
- `build.mjs`：用 esbuild 注入 `import.meta.env.*` 并打成 CJS。
- `run_drawer.mjs` 里对 `echarts-for-react` 做了一次 require 解包：该包是 CJS 单一
  `default` 导出，在 Node 下 require 得到 `{ default: fn }`，不还原会导致 React 收到 object。
  这是 **Node 测试环境**的互操作需要，Vite 构建产物不受影响。
- `screenshot.mjs` 需要 `npm run preview` 已在 4173 端口运行，且依赖 puppeteer。
