// Order 26 (I-20) e2e entry:
//  A) ErrorBoundary catches a child component render throw (no blank screen; shows fallback UI + retry button)
//  B) react-query against /history: auto-retry on failure + same-key cache hit (no further backend calls)
import { createRoot } from 'react-dom/client';
import { ErrorBoundary } from 'react-error-boundary';
import { QueryClientProvider, useQuery } from '@tanstack/react-query';
import ErrorFallback from '../src/ErrorFallback';
import { makeQueryClient } from '../src/lib/queryClient';
import { fetchRobotHistory, robotHistoryKey } from '../src/api/robotHistory';

declare global {
  interface Window {
    __MOUNT_BOUNDARY__: () => () => void;
    __MOUNT_QUERY__: () => () => void;
  }
}

// —— A) Error boundary: the child component throws unconditionally ——
function Boom(): JSX.Element {
  throw new Error('simulated render exception(order26-e2e)');
}

window.__MOUNT_BOUNDARY__ = () => {
  const qc = makeQueryClient();
  const root = createRoot(document.getElementById('root')!);
  root.render(
    <ErrorBoundary FallbackComponent={ErrorFallback}>
      <QueryClientProvider client={qc}>
        <Boom />
      </QueryClientProvider>
    </ErrorBoundary>,
  );
  return () => root.unmount();
};

// —— B) react-query: expose queryClient and request count for asserting retry and cache behavior ——
function HistoryProbe() {
  const q = useQuery({
    queryKey: robotHistoryKey('humanoid-042'),
    queryFn: ({ signal }) => fetchRobotHistory('humanoid-042', signal),
  });
  return (
    <div data-testid="state">
      {q.isSuccess ? `ok:${q.data.length}` : q.isError ? 'err' : 'loading'}
    </div>
  );
}

window.__MOUNT_QUERY__ = () => {
  const qc = makeQueryClient();
  (window as unknown as { __QC__: unknown }).__QC__ = qc;
  const root = createRoot(document.getElementById('root')!);
  root.render(
    <QueryClientProvider client={qc}>
      <HistoryProbe />
    </QueryClientProvider>,
  );
  return () => root.unmount();
};
export {};
