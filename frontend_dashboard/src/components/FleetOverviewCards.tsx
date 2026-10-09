// spec/03 §5.1 —— 100 卡片墙
// grid-cols-5 md:grid-cols-10 gap-2，每卡 aspect-[4/3]，颜色全部走 theme/tokens。

import { memo, useMemo } from 'react';
import { COLORS, FONT, STATUS_COLOR, STATUS_LABEL, batteryColor, tempColor } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import type { RobotTelemetry } from '../types/robot';

/** 卡片只吃自己的机器人对象，配合 memo 让「本帧没变的卡」不重渲染 */
interface RobotCardProps {
  robot: RobotTelemetry;
  selected: boolean;
  onOpen: (robotId: string) => void;
}

const RobotCard = memo(function RobotCard({ robot, selected, onOpen }: RobotCardProps) {
  const statusColor = STATUS_COLOR[robot.status_code];
  const battColor = batteryColor(robot.battery_pct);
  const tempClr = robot.status_code === 0 ? COLORS.statusOffline : tempColor(robot.max_joint_temp);
  const battWidth = Math.max(0, Math.min(100, robot.battery_pct));

  return (
    <button
      type="button"
      onClick={() => onOpen(robot.robot_id)}
      className="flex flex-col text-left overflow-hidden transition-colors"
      style={{
        backgroundColor: COLORS.surface,
        border: `1px solid ${selected ? COLORS.accent : COLORS.border}`,
        borderRadius: '10px',
        aspectRatio: '4 / 3',
      }}
    >
      {/* 1. 顶部状态条 */}
      <div className="h-1 w-full shrink-0" style={{ backgroundColor: statusColor }} />

      <div className="flex flex-col flex-1 min-h-0 px-2 py-1 gap-0.5">
        {/* 2. Robot ID（H-042 短名，13px/600） */}
        <span
          className="truncate"
          style={{ ...FONT.cardId, color: robot.status_code === 0 ? COLORS.textSecondary : COLORS.textPrimary }}
        >
          H-{robot.robot_id.replace(/^humanoid-/, '')}
        </span>

        {/* 3. 电池条（宽=电量%） */}
        <div className="flex items-center gap-1">
          <div className="flex-1 h-1.5 rounded-sm overflow-hidden" style={{ backgroundColor: COLORS.border }}>
            <div className="h-full" style={{ width: `${battWidth}%`, backgroundColor: battColor }} />
          </div>
          <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            {robot.battery_pct.toFixed(0)}%
          </span>
        </div>

        {/* 4. 最高关节温度（16px/700，按温区着色） */}
        <span className="tabular-nums" style={{ fontSize: '16px', fontWeight: 700, color: tempClr }}>
          {robot.status_code === 0 ? '--' : `${robot.max_joint_temp.toFixed(0)}°C`}
        </span>

        {/* 5. 状态文字 */}
        <span className="mt-auto" style={{ ...FONT.metric, color: statusColor }}>
          {STATUS_LABEL[robot.status_code]}
        </span>
      </div>
    </button>
  );
});

export default function FleetOverviewCards() {
  // 只订阅 robots 这一个字段；100ms 批量刷库时本组件最多 10 次/秒重渲染
  const robots = useRobotStore((s) => s.robots);
  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  const list = useMemo(() => Array.from(robots.values()), [robots]);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2">
      <div className="grid grid-cols-5 md:grid-cols-10 gap-2">
        {list.map((robot) => (
          <RobotCard
            key={robot.robot_id}
            robot={robot}
            selected={robot.robot_id === selectedRobotId}
            onOpen={selectRobot}
          />
        ))}
      </div>
    </div>
  );
}
