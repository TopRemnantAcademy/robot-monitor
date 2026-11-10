// Shared test render utility (Order 26 / Issue #78 fix).
//
// Order 26 wired @tanstack/react-query (useQuery) into components like RobotDetailDrawer,
// but the original test base (src/test/setup.ts) only handled the msw lifecycle and did not wrap a QueryClientProvider,
// so any case rendering a "useQuery component" threw
//   "No QueryClient set, use QueryClientProvider to set one"
// —— i.e. `npm test` on main was 8/8 all red, breaking every PR's test gate.
//
// This file exports a render already wrapped in QueryClientProvider. Test cases only need to change
//   `import { render } from '@testing-library/react'`
// to import render from this file; the rest of the RTL API (screen / waitFor / within etc.)
// is still taken from @testing-library/react, so call sites need no change.
import { render as rtlRender } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement } from 'react';

const queryClient = new QueryClient({
  defaultOptions: {
    // Don't retry endlessly in tests, to avoid cases hanging on a missing network mock
    queries: { retry: false },
  },
});

export function render(ui: ReactElement) {
  return rtlRender(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}
