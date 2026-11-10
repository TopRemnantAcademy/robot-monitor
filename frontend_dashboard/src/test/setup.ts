// Global component-test base (Order 24 T2; Order 26's react-query integration later patched the Provider via Issue #78).
//
// Four things:
//   1) jest-dom extended assertions (toBeInTheDocument / toHaveTextContent etc.)
//   2) Globally wrap QueryClientProvider —— components use useQuery (RobotDetailDrawer.tsx:57),
//      and useQuery reads the client from React Context; without a Provider it throws immediately
//      "No QueryClient set, use QueryClientProvider to set one".
//      Wrapping once in the base fixes all cases in one shot (Issue #78 acceptance's preferred approach),
//      less error-prone than writing a local Provider around each of the 8 render() calls.
//   3) msw lifecycle: intercept the real fetch issued by components (/robots/{id}/history)
//      —— using msw rather than vi.spyOn(global,'fetch') is so that a "wrong URL concatenation" can also be caught:
//      the interceptor matches on the real URL + method, so a wrong concatenation means no handler hits and the test goes red.
//   4) afterEach cleanup: unmount the React tree + invalidate in-flight queries + reset the store + clear the token
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

// —— QueryClientProvider (Issue #78)——
//
// WHY CREATE A NEW CLIENT PER CASE INSTEAD OF SHARING ONE GLOBALLY
// QueryClient holds the **query cache**; sharing across cases leaks: the previous case's
// /history result for humanoid-042 would feed the next case, and within `staleTime=10s` (queryClient.ts:24)
// it hits the cache directly and never issues a request —— cases like "history empty / 500 error / Authorization header"
// would turn into false greens.
//
// WHY WRAP AT THE RENDER EXIT RATHER THAN A GLOBALLY RESIDENT PROVIDER
// cleanup() unmounts the React tree in afterEach → the Provider's useEffect cleanup decrements the client count by 1
// (TanStack Query v5's QueryClientProvider still does this, see QueryClientProvider.js:46-49).
// If the Provider were resident, the count would climb to N with the number of cases, and each client's focus/online
// subscriptions would never release, leaving closures pointing at an unmounted tree. So wrapping at the render layer
// **mounts/unmounts per case**, keeping the count strictly 0↔1 balanced.
//
// WHY USE RENDER'S wrapper OPTION RATHER THAN RENDERING A WRAPPER LAYER MYSELF
// @testing-library/react's render() honors the second argument's `wrapper` and renders it inside its own act() wrapper.
// This way the Provider and the component share the same act scope and no "update not wrapped in act(...)" warning appears;
// and unmount only happens in cleanup() (vitest's restoreMocks is unaffected).
//
// WHY USE vi.mock TO WRAP render RATHER THAN Object.assign TO OVERRIDE THE MODULE EXPORT
// Vite's ESM transform makes namespace exports read-only getters, so Object.assign would throw
// "Cannot set property render of #<Object> which has only a getter".
// vi.mock is hoisted before the import as a module replacement, exactly designed for this kind of "change third-party module behavior".
// Only swap render; the other exports (screen / within / waitFor / cleanup …) pass through untouched,
// avoiding tests importing both the base and the library and getting two instances.
vi.mock('@testing-library/react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@testing-library/react')>();
  return {
    ...actual,
    // Call sites that explicitly pass a wrapper can still override; other cases need zero change.
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

// setup.ts is .ts (not .tsx), so createElement is used here instead of JSX.
function QueryClientWrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children);
}

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));

afterEach(() => {
  cleanup();
  server.resetHandlers();
  // The store is a module-level singleton: it must be reset between cases, otherwise the previous case's selectedRobotId leaks into the next
  useRobotStore.setState({
    selectedRobotId: null,
    wsStatus: 'connecting',
    alerts: [],
  });
  clearToken();
  // react-query's QueryClient is also a module-level singleton: swap in a brand-new one (empty cache, no in-flight requests).
  // The old one is clear()'d to drop cache entries first, then left for GC, so the previous case's response can't leak in as a false green.
  queryClient.clear();
  queryClient = makeQueryClient();
});

afterAll(() => server.close());
