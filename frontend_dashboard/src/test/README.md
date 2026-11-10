# Frontend component tests (Order 24 T2)

## How to run

```bash
cd frontend_dashboard
npm install
npm test          # = vitest run (use this for CI)
npm run test:watch
```

## Structure

| File | Purpose |
|---|---|
| `vitest.config.ts` | test config (independent of `vite.config.ts`, see in-file comments) |
| `src/test/setup.ts` | jest-dom assertions + msw lifecycle + per-case reset of store/token |
| `src/test/msw/handlers.ts` | fake REST contract implementation (URLs byte-aligned with `RobotHistoryResponse`) |
| `src/test/msw/server.ts` | msw Node-side `setupServer` |
| `src/components/__tests__/*.test.tsx` | component tests |

## Two environment pitfalls (both already worked around in config; read before changing config)

1. **The environment uses happy-dom instead of jsdom**: jsdom overrides its own `AbortController`, while Node's built-in fetch (undici) only recognizes its own `AbortSignal`, causing requests carrying a `signal` in components to throw `Expected signal ... to be an instance of AbortSignal`. See `vitest.config.ts` comments for details.
2. **ECharts is mocked**: jsdom/happy-dom have no canvas 2D context, so really rendering ECharts errors out. In tests, `echarts-for-react/lib/core` is swapped for a stub that dumps `option` to JSON, so assertions land on "the chart model the component computed" (the pure logic that should be tested) rather than on ECharts pixels.
