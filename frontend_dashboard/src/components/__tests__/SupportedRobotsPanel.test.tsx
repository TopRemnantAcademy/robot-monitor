// SupportedRobotsPanel 契约测试：验证「支持的机型」在 UI 中渲染，
// 且 admin 角色额外看到机群管理明细（厂商 / 固件 / 工位）。
//
// 用 src/test/render.tsx 的包裹版 render（已含 QueryClientProvider），
// msw 由 setup.ts 统一启动，handler 见 src/test/msw/handlers.ts。

import { beforeEach, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../../test/render';
import { useAuthStore } from '../../auth/authStore';
import SupportedRobotsPanel from '../SupportedRobotsPanel';

beforeEach(() => {
  useAuthStore.setState({ status: 'authenticated', email: 'u@e.com', role: 'viewer' });
});

test('viewer：渲染支持的机型卡片（名称 + 机型标识 + 在册/在线数）', async () => {
  render(<SupportedRobotsPanel />);

  // 标题恒渲染
  expect(screen.getByText('Supported models')).toBeInTheDocument();

  // msw 下发的样例数据应渲染出机型名称与标识
  await waitFor(() => {
    expect(screen.getByText('Humanoid 24-DOF')).toBeInTheDocument();
  });
  expect(screen.getByText('HUMANOID-24DOF')).toBeInTheDocument();

  // viewer 不应看到管理视图标记与机群管理明细
  expect(screen.queryByText('Admin view')).not.toBeInTheDocument();
  expect(screen.queryByText(/Stations:/)).not.toBeInTheDocument();
});

test('admin：额外展示机群管理明细（管理视图标记 + 厂商/固件/工位）', async () => {
  useAuthStore.setState({ status: 'authenticated', email: 'a@e.com', role: 'admin' });
  render(<SupportedRobotsPanel />);

  await waitFor(() => {
    expect(screen.getByText('Humanoid 24-DOF')).toBeInTheDocument();
  });

  expect(screen.getByText('Admin view')).toBeInTheDocument();
  expect(screen.getByText(/Stations: 10/)).toBeInTheDocument();
  expect(screen.getByText(/Firmware: 1.0.0/)).toBeInTheDocument();
});
