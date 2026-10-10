/// <reference types="vitest/config" />
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 组件测试配置（订单 24 T2）。
//
// 刻意**独立于** vite.config.ts：vitest 的 test 段与生产构建无关，
// 混在一起会让 `vite build` 也背上测试依赖的解析开销。
// 插件与生产一致（@vitejs/plugin-react，自动 JSX runtime），
// 保证测的是真实组件，而不是另一份转译形态的副本。
//
// 【为什么用 happy-dom 而不是 jsdom】
// 实测（本机，`npx vitest run`）：jsdom 会把自己的 `AbortController`/`AbortSignal`
// 覆盖到全局，而 Node 自带的 fetch（undici）只接受**自己**的 AbortSignal 实例，
// 于是 `fetch(url, { signal })` 直接抛
//   TypeError: RequestInit: Expected signal ("AbortSignal {}") to be an instance of AbortSignal
// Drawer 的 history 请求带 `signal`（组件内部用 AbortController 取消上一台机器人的请求），
// 在 jsdom 下因此永远挂起，全部 fetch 相关用例超时。换 happy-dom 后其 AbortSignal
// 与 undici 兼容，8/8 通过。（这是环境层问题，与组件实现无关，不改产品代码。）
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'happy-dom',
    globals: true,                       // 免去每个文件 import { describe, it, expect }
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    restoreMocks: true,
  },
});
