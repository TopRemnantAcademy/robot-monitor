// 组件测试全局基座（订单 24 T2）。
//
// 三件事：
//   1) jest-dom 扩展断言（toBeInTheDocument / toHaveTextContent 等）
//   2) msw 生命周期：拦截组件里真实发出的 fetch（/robots/{id}/history）
//      —— 用 msw 而不是 vi.spyOn(global,'fetch')，是为了让「URL 拼错」也能被测出来：
//      拦截器按真实 URL + method 匹配，拼错了就没有 handler 命中，测试会红。
//   3) afterEach 清理：卸载 React 树 + 复位 store + 清 token
import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { cleanup } from '@testing-library/react';

import { server } from './msw/server';
import { useRobotStore } from '../store/robotStore';
import { clearToken } from '../auth/session';

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
});

afterAll(() => server.close());
