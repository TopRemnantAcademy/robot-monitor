# 前端组件测试（订单 24 T2）

## 跑法

```bash
cd frontend_dashboard
npm install
npm test          # = vitest run（CI 用这条）
npm run test:watch
```

## 结构

| 文件 | 作用 |
|---|---|
| `vitest.config.ts` | 测试配置（独立于 `vite.config.ts`，见文件内注释） |
| `src/test/setup.ts` | jest-dom 断言 + msw 生命周期 + 每例复位 store/token |
| `src/test/msw/handlers.ts` | REST 契约的假实现（URL 逐字对齐 `RobotHistoryResponse`） |
| `src/test/msw/server.ts` | msw Node 侧 `setupServer` |
| `src/components/__tests__/*.test.tsx` | 组件测试 |

## 两个环境坑（都已在配置里绕开，改配置前先读）

1. **环境用 happy-dom 而非 jsdom**：jsdom 覆盖了自己的 `AbortController`，
   而 Node 自带 fetch（undici）只认自己的 `AbortSignal`，导致组件里带
   `signal` 的请求直接抛 `Expected signal ... to be an instance of AbortSignal`。
   详见 `vitest.config.ts` 注释。

2. **ECharts 被 mock**：jsdom/happy-dom 都没有 canvas 2D 上下文，真渲染 ECharts 会报错。
   测试里把 `echarts-for-react/lib/core` 换成把 `option` dump 成 JSON 的替身，
   于是断言落在「组件算出的图表模型」这一层（最该测的纯逻辑），而非 ECharts 的像素。
