// spec/03 §4 布局 —— 三区网格，整体 h-screen flex flex-col overflow-hidden（禁止页面级滚动）
//
// 订单 15：最外层套 LoginGate。未登录时**不渲染**任何看板组件，
// 也就不会建立 WS 连接、不会发 REST 请求 —— 未授权者连"看板长什么样"都拿不到。
//
// 订单 26（I-20）：App 外层套 ErrorBoundary（渲染异常兜底，不再白屏）+
// QueryClientProvider（服务端状态缓存/重试；zustand 继续管 WS 客户端状态，两者互补）。

import { useEffect, useState } from 'react';
import { ErrorBoundary } from 'react-error-boundary';
import { QueryClientProvider } from '@tanstack/react-query';
import HealthSummaryHeader from './components/HealthSummaryHeader';
import FleetOverviewCards from './components/FleetOverviewCards';
import FleetHealthAnalytics from './components/FleetHealthAnalytics';
import SupportedRobotsPanel from './components/SupportedRobotsPanel';
import AlertLogTable from './components/AlertLogTable';
import RobotDetailDrawer from './components/RobotDetailDrawer';
import LoginGate from './auth/LoginGate';
import { setUnauthorizedHandler } from './auth/api';
import { useAuthStore } from './auth/authStore';
import { useRobotWebSocket } from './hooks/useRobotWebSocket';
import { startObservabilityReport } from './lib/observability';
import { COLORS } from './theme/tokens';
import { makeQueryClient } from './lib/queryClient';
import ErrorFallback from './ErrorFallback';

/** 已登录后的看板主体。只有在这里才建立 WS —— 未登录时不连。 */
function Dashboard() {
  // 全局唯一的 WS 接入点：100ms 节流刷库（spec/03 §6）
  useRobotWebSocket();

  // 订单 16 T2：每 5s 打印一行重渲染摘要（wsMsgs / storeCommits / cardRenders / ratio）。
  // 挂在 Dashboard（而非 App）是刻意的：未登录时 LoginGate 不渲染 Dashboard，
  // 也就不会起一个只打印全 0 的定时器污染控制台。
  useEffect(() => startObservabilityReport(), []);

  return (
    <div
      className="h-screen w-screen flex flex-col overflow-hidden"
      style={{ backgroundColor: COLORS.bgBase, color: COLORS.textPrimary }}
    >
      <HealthSummaryHeader />
      {/* UI 强化：机群级分布 + 趋势层（见 FleetHealthAnalytics 顶部说明）。
          fixed-height strip（shrink-0），卡片墙 flex-1 自适应收缩，页面仍禁滚动。 */}
      <FleetHealthAnalytics />
      <SupportedRobotsPanel />
      <FleetOverviewCards />
      <AlertLogTable />
      <RobotDetailDrawer />
    </div>
  );
}

export default function App() {
  const bootstrap = useAuthStore((s) => s.bootstrap);

  // QueryClient 只在 App 生命周期内建一次：用 useState 惰性初始化，
  // 避免每次 render 重建（重建会清空缓存）。React 18 StrictMode 双调用也只留一份。
  const [queryClient] = useState(makeQueryClient);

  useEffect(() => {
    // 401 的统一出口：任何业务请求收到 401 都清 token 回登录页。
    // 注册在 api 层而不是每个调用点上，避免漏掉新加的请求。
    setUnauthorizedHandler(() => useAuthStore.getState().markAnonymous());
    void bootstrap();
    return () => setUnauthorizedHandler(null);
  }, [bootstrap]);

  return (
    // ErrorBoundary 在 Provider 之外：Provider 自身异常也能被兜住。
    <ErrorBoundary FallbackComponent={ErrorFallback}>
      <QueryClientProvider client={queryClient}>
        <LoginGate>
          <Dashboard />
        </LoginGate>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
