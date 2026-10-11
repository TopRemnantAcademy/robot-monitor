/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 组件测试配置（订单 24 T2；Issue #78 把环境从 happy-dom 换回 jsdom）。
//
// 刻意**独立于** vite.config.ts：vitest 的 test 段与生产构建无关，
// 混在一起会让 `vite build` 也背上测试依赖的解析开销。
// 插件与生产一致（@vitejs/plugin-react，自动 JSX runtime），
// 保证测的是真实组件，而不是另一份转译形态的副本。
//
// 【为什么是 jsdom 而不是 happy-dom（Issue #78 的实测结论）】
// 订单 24 曾选 happy-dom，理由是「jsdom 的 AbortSignal 与 undici 不兼容，
// 带 signal 的 fetch 会挂起」。该理由在本仓**当前锁定的依赖组合下已不成立**：
// 实测（Node 20/npm 10，`npm ci` 后 `npx vitest run`）反而是 happy-dom 出了问题 ——
// msw 2.15.0（package-lock 锁定值，非 package.json 的 ^2.7.0 下限）通过
// `@mswjs/interceptors` 的 FetchResponse extends globalThis.Response 回填响应，
// 而 happy-dom 的 Response 构造器会先消费传入的 body stream，导致读响应体时抛
//   TypeError: Invalid state: ReadableStream is locked
// 于是所有**需要读取响应体**的用例（history 折线 / 空数据 / 500 错误态）恒失败，
// 只有只看请求头、不读 body 的用例能过 —— 正是 8/8 红里最难查的那一半。
// jsdom 下 response body 可正常读取，AbortSignal 也不再撞 undici：
// 换回 jsdom 后 8/8 全绿（500 用例见下方 timeout 说明）。
//
// 这是**环境层**问题，不改产品代码、不改 msw handler 契约（handler 仍用
// HttpResponse.json 输出 spec/00 §5.3 的 RobotHistoryResponse 字段）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,                       // 免去每个文件 import { describe, it, expect }
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    restoreMocks: true,
  },
});
