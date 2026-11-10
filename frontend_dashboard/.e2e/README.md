# .e2e — frontend integration verification (not part of the production build)

Runs real product code (`src/hooks/useRobotWebSocket.ts`, `src/store/robotStore.ts`, `src/components/*`) on jsdom / headless Chromium to verify the hard contracts of spec/03.

## Running

```bash
cd frontend_dashboard
npm install
bash .e2e/run_all.sh        # one-click full run (5 items)
```

Run individually:

```bash
E2E_WS_PORT=8123 node .e2e/build.mjs   # first bundle src into Node CJS
node .e2e/run.mjs                       # WS flood + type three branches + 100ms throttle
node .e2e/run_reconnect.mjs             # exponential backoff 1s→2s→4s→8s
E2E_API_BASE=http://localhost:8201 node .e2e/build.mjs
node .e2e/run_drawer.mjs                # REST history → ECharts
node .e2e/run_offline.mjs               # no data → 100 offline placeholder cards (needs vite preview on 4173)
node .e2e/screenshot.mjs                # real-browser screenshot (with fake backend)
```

## Notes

- `entry.tsx` / `entry_drawer.tsx`: minimal entry points that mount the product components onto jsdom.
- `build.mjs`: uses esbuild to inject `import.meta.env.*` and bundle to CJS.
- `run_drawer.mjs` does a one-time require unwrap of `echarts-for-react`: the package is a CJS single `default` export, and under Node `require` yields `{ default: fn }`; without restoring it React would receive an object. This is an **interop need of the Node test environment**; the Vite build output is unaffected.
- `screenshot.mjs` requires `npm run preview` already running on port 4173, and depends on puppeteer.
