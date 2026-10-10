// spec/03 §4 布局 —— 三区网格，整体 h-screen flex flex-col overflow-hidden（禁止页面级滚动）
//
// 订单 15：最外层套 LoginGate。未登录时**不渲染**任何看板组件，
// 也就不会建立 WS 连接、不会发 REST 请求 —— 未授权者连"看板长什么样"都拿不到。
import { useEffect } from 'react';
import HealthSummaryHeader from './components/HealthSummaryHeader';
import FleetOverviewCards from './components/FleetOverviewCards';
import AlertLogTable from './components/AlertLogTable';
import RobotDetailDrawer from './components/RobotDetailDrawer';
import LoginGate from './auth/LoginGate';
import { setUnauthorizedHandler } from './auth/api';
import { useAuthStore } from './auth/authStore';
import { useRobotWebSocket } from './hooks/useRobotWebSocket';
import { COLORS } from './theme/tokens';

/** 已登录后的看板主体。只有在这里才建立 WS —— 未登录时不连。 */
function Dashboard() {
  // 全局唯一的 WS 接入点：100ms 节流刷库（spec/03 §6）
  useRobotWebSocket();

  return (
    <div
      className="h-screen w-screen flex flex-col overflow-hidden"
      style={{ backgroundColor: COLORS.bgBase, color: COLORS.textPrimary }}
    >
      <HealthSummaryHeader />
      <FleetOverviewCards />
      <AlertLogTable />
      <RobotDetailDrawer />
    </div>
  );
}

export default function App() {
  const bootstrap = useAuthStore((s) => s.bootstrap);

  useEffect(() => {
    // 401 的统一出口：任何业务请求收到 401 都清 token 回登录页。
    // 注册在 api 层而不是每个调用点上，避免漏掉新加的请求。
    setUnauthorizedHandler(() => useAuthStore.getState().markAnonymous());
    void bootstrap();
    return () => setUnauthorizedHandler(null);
  }, [bootstrap]);

  return (
    <LoginGate>
      <Dashboard />
    </LoginGate>
  );
}
