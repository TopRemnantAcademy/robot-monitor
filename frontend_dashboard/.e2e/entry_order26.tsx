// 订单 26（I-20）e2e 入口：
//  A) ErrorBoundary 兜住子组件渲染抛错（不白屏，显示兜底 UI + 重试按钮）
//  B) react-query 对 /history：失败自动重试 + 同 key 命中缓存（不再打后端）
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

// —— A) 错误边界：子组件无条件抛错 ——
function Boom(): JSX.Element {
  throw new Error('模拟渲染异常(order26-e2e)');
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

// —— B) react-query：暴露 queryClient 与请求计数，供断言重试与缓存 ——
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
