// SupportedRobotsPanel contract test: verify "supported models" render in the UI,
// and the admin role additionally sees fleet management details (vendor / firmware / stations).
//
// Uses the wrapped render from src/test/render.tsx (already includes QueryClientProvider),
// msw is started centrally by setup.ts, handler see src/test/msw/handlers.ts.

import { beforeEach, expect, test } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../../test/render';
import { useAuthStore } from '../../auth/authStore';
import SupportedRobotsPanel from '../SupportedRobotsPanel';

beforeEach(() => {
  useAuthStore.setState({ status: 'authenticated', email: 'u@e.com', role: 'viewer' });
});

test('viewer: render supported models card (name + model id + registered/online count)', async () => {
  render(<SupportedRobotsPanel />);

  // Title always renders
  expect(screen.getByText('Supported models')).toBeInTheDocument();

  // msw sample data should render model name and id
  await waitFor(() => {
    expect(screen.getByText('Humanoid 24-DOF')).toBeInTheDocument();
  });
  expect(screen.getByText('HUMANOID-24DOF')).toBeInTheDocument();

  // viewer should not see admin-view marker and fleet management details
  expect(screen.queryByText('Admin view')).not.toBeInTheDocument();
  expect(screen.queryByText(/Stations:/)).not.toBeInTheDocument();

  // Single model must use singular '1 model' (not '1 models') —— i18n plural regression
  await waitFor(() => {
    expect(screen.getByText('1 model · 100 robots registered')).toBeInTheDocument();
  });
  expect(screen.queryByText(/1 models/)).not.toBeInTheDocument();
});

test('admin: additionally show fleet management details (admin-view marker + vendor/firmware/stations)', async () => {
  useAuthStore.setState({ status: 'authenticated', email: 'a@e.com', role: 'admin' });
  render(<SupportedRobotsPanel />);

  await waitFor(() => {
    expect(screen.getByText('Humanoid 24-DOF')).toBeInTheDocument();
  });

  expect(screen.getByText('Admin view')).toBeInTheDocument();
  expect(screen.getByText(/Stations: 10/)).toBeInTheDocument();
  expect(screen.getByText(/Firmware: 1.0.0/)).toBeInTheDocument();
});
