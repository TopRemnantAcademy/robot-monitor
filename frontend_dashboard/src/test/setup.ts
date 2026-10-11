// 组件测试全局基座（订单 24 T2；订单 26 的 react-query 接入后由 Issue #78 补齐 Provider）。
//
// 四件事：
//   1) jest-dom 扩展断言（toBeInTheDocument / toHaveTextContent 等）
//   2) 全局包裹 QueryClientProvider —— 组件用 useQuery（RobotDetailDrawer.tsx:57），
//      而 useQuery 走 React Context 取 client，没有 Provider 就直接抛
//      "No QueryClient set, use QueryClientProvider to set one"。
//      在基座里包一次，一次性修复所有用例（Issue #78 验收标准首选方案），
//      比在 8 处 render() 外层各写一层局部 Provider 更不易漏。
//   3) msw 生命周期：拦截组件里真实发出的 fetch（/robots/{id}/history）
//      —— 用 msw 而不是 vi.spyOn(global,'fetch')，是为了让「URL 拼错」也能被测出来：
//      拦截器按真实 URL + method 匹配，拼错了就没有 handler 命中，测试会红。
//   4) afterEach 清理：卸载 React 树 + 作废在途 query + 复位 store + 清 token
import '@testing-library/jest-dom/vitest';
import { createElement } from 'react';
import type { ReactNode } from 'react';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { QueryClientProvider } from '@tanstack/react-query';
import { cleanup } from '@testing-library/react';

import { server } from './msw/server';
import { useRobotStore } from '../store/robotStore';
import { clearToken } from '../auth/session';
import { makeQueryClient } from '../lib/queryClient';

// —— QueryClientProvider（Issue #78）——
//
// 【为什么每例新建一个 client，而不是全局共享一个】
// QueryClient 持有**查询缓存**，跨用例共享会串味：上一个用例对 humanoid-042 的
// /history 结果会喂给下一个用例，`staleTime=10s`（queryClient.ts:24）内直接命中缓存、
// 根本不发请求 —— 那些「history 为空 / 500 错误 / Authorization 头」的用例会变成假绿。
//
// 【为何在 render 出口包，而不是全局常驻一个 Provider】
// cleanup() 在 afterEach 卸载 React 树 → Provider 的 useEffect 清理函数给 client 计数 -1
// （TanStack Query v5 的 QueryClientProvider 仍旧如此，见 QueryClientProvider.js:46-49）。
// 若 Provider 常驻，计数会随用例数一路涨到 N，每个 client 的 focus/online 订阅永不释放，
// 还留下指向已卸载树的闭包。所以在 render 这一层**随用例挂载/卸载**，计数严格 0↔1 平衡。
//
// 【为何用 render 的 wrapper 选项，而不是自己 render 一层 Wrapper】
// @testing-library/react 的 render() 认第二个参数的 `wrapper`，并在自己的 act() 包裹内
// 渲染。这样 Provider 与组件处于同一个 act 作用域，不会冒出 "update not wrapped in act(...)"
// 警告；且卸载只发生在 cleanup() 里（vitest 的 restoreMocks 不受影响）。
//
// 【为何用 vi.mock 包 render，而不是 Object.assign 覆盖模块导出】
// Vite 的 ESM 转换把 namespace 导出设成了只读 getter，Object.assign 会直接抛
// "Cannot set property render of #<Object> which has only a getter"。
// vi.mock 是 hoisted 到 import 之前的模块替换，正是为这种「改第三方模块行为」设计的。
// 只换 render，其余导出（screen / within / waitFor / cleanup …）原样透传，
// 避免测试里同时 import 基座与库、拿到两套实例。
vi.mock('@testing-library/react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@testing-library/react')>();
  return {
    ...actual,
    // 显式传 wrapper 的调用点仍可覆盖，其余用例零改动。
    render: (
      ui: Parameters<typeof actual.render>[0],
      options?: Parameters<typeof actual.render>[1],
    ) =>
      actual.render(ui, {
        ...options,
        wrapper: options?.wrapper ?? QueryClientWrapper,
      }),
  };
});

let queryClient = makeQueryClient();

// setup.ts 是 .ts（非 .tsx），这里用 createElement 而不是 JSX。
function QueryClientWrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));

afterEach(() => {
  cleanup();
  server.resetHandlers();
  // store 是模块级单例：用例之间必须复位，否则上一个用例的 selectedRobotId 会漏给下一个
  useRobotStore.setState({
    selectedRobotId: null,
    wsStatus: 'connecting',
    alerts: [],
  });
  clearToken();
  // react-query 的 QueryClient 同样是模块级单例：换一个全新的（缓存空、无在途请求）。
  // 旧的先 clear() 去掉缓存条目，再让 GC 回收，避免上一个用例的 response 漏进来做假绿。
  queryClient.clear();
  queryClient = makeQueryClient();
});

afterAll(() => server.close());
