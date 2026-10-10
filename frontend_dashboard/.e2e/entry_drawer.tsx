import { createRoot } from 'react-dom/client';
import { useRobotStore } from '../src/store/robotStore';
import RobotDetailDrawer, { API_BASE } from '../src/components/RobotDetailDrawer';

import { JOINT_NAMES } from '../src/types/robot';

declare global { interface Window { __D__: any; __MOUNT__: () => void } }
window.__D__ = { API_BASE, JOINT_NAMES };
window.__MOUNT__ = () => {
  // 预置一台机器人并打开 Drawer
  const st = useRobotStore.getState();
  st.applyTelemetryBatch([{
    robot_id: 'humanoid-042', status_code: 2, battery_pct: 18.4, max_joint_temp: 66.2,
    joint_temperatures: Object.fromEntries(JOINT_NAMES.map((j, i) => [j, 45 + i])),
    joint_torques: Object.fromEntries(JOINT_NAMES.map((j, i) => [j, i * 0.1])),
    joint_positions: Object.fromEntries(JOINT_NAMES.map((j, i) => [j, i * 0.01])),
    error_code: 0, ts: Date.now(),
  }]);
  st.selectRobot('humanoid-042');
  createRoot(document.getElementById('root')!).render(<RobotDetailDrawer />);
};
export {};
