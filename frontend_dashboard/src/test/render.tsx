// 测试共享渲染工具（订单 26 / Issue #78 修复）。
//
// 订单 26 给 RobotDetailDrawer 等组件接入了 @tanstack/react-query（useQuery），
// 但原测试基座（src/test/setup.ts）只管 msw 生命周期，没有包裹 QueryClientProvider，
// 导致任何渲染「使用 useQuery 组件」的用例都抛
//   "No QueryClient set, use QueryClientProvider to set one"
// —— 即 main 上 `npm test` 8/8 全红，破坏了所有 PR 的测试门禁。
//
// 本文件导出一个已包裹 QueryClientProvider 的 render。测试用例只需把
//   `import { render } from '@testing-library/react'`
// 改为从本文件 import render，其余 RTL API（screen / waitFor / within 等）
// 仍从 @testing-library/react 取，调用点无需改。
import { render as rtlRender } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement } from 'react';

const queryClient = new QueryClient({
  defaultOptions: {
    // 测试里不要无限重试，避免用例因网络 mock 缺失而长时间挂起
    queries: { retry: false },
  },
});

export function render(ui: ReactElement) {
  return rtlRender(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}
