// spec/03 §4 布局 —— 三区网格，整体 h-screen flex flex-col overflow-hidden（禁止页面级滚动）
import HealthSummaryHeader from './components/HealthSummaryHeader';
import FleetOverviewCards from './components/FleetOverviewCards';
import AlertLogTable from './components/AlertLogTable';
import RobotDetailDrawer from './components/RobotDetailDrawer';
import { useRobotWebSocket } from './hooks/useRobotWebSocket';
import { COLORS } from './theme/tokens';

export default function App() {
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
