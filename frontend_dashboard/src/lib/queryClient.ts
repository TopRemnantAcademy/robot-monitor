// Order 26 (I-20): server-side state layer —— TanStack Query.
//
// Responsibility boundary (stated in external/INTEGRATION-PLAN.md §8.10):
//   - Query manages **server** state: caching, deduplication, retry, and invalidation of history queries.
//   - Zustand manages **client** state: the real-time telemetry and alerts after the 100ms WS throttle (robotStore.ts unchanged).
// The two complement each other, **not replace each other** —— the earlier "don't upgrade zustand to v5" decision stands.

import { QueryClient } from '@tanstack/react-query';

/**
 * Global QueryClient.
 *
 * Default-value trade-offs:
 *   - retry: 2 —— history queries are retryable idempotent GETs; but 4xx (including 401/404) is not retried,
 *     401 is handled by the caller via notifyUnauthorized() sending the user to the login page; retrying would only repeat the login prompt.
 *   - staleTime: 10s —— aligned with the 10s resolution of history points: reopening within a bucket doesn't re-hit the backend.
 *   - gcTime: 5min —— after closing the Drawer the cache is kept for 5 minutes, so switching back and forth between robots needs no cold start.
 *   - refetchOnWindowFocus: false —— the big screen is permanently foreground; window-focus jitter should not trigger a refetch.
 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // HTTP 4xx is a deterministic failure (auth/routing/params); retrying is pointless.
          const status = (error as { status?: number })?.status;
          if (typeof status === 'number' && status >= 400 && status < 500) return false;
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 8000),
      },
    },
  });
}
