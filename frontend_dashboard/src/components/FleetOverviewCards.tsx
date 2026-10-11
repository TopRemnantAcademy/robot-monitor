// spec/03 §5.1 —— 100 卡片墙
// grid-cols-5 md:grid-cols-10 gap-2，每卡 aspect-[4/3]，颜色全部走 theme/tokens。

import { memo, useMemo, useRef } from 'react';
import {
  COLORS,
  DISPLAY_COLOR,
  DISPLAY_LABEL,
  FONT,
  batteryColor,
  hasEverReported,
  isOfflineDisplay,
  robotDisplayState,
  tempColor,
} from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { countCardRender } from '../lib/observability';
import type { RobotTelemetry } from '../types/robot';

/** 卡片只吃自己的机器人对象，配合 memo 让「本帧没变的卡」不重渲染 */
interface RobotCardProps {
  robot: RobotTelemetry;
  selected: boolean;
  onOpen: (robotId: string) => void;
}

const RobotCard = memo(function RobotCard({ robot, selected, onOpen }: RobotCardProps) {
  // ISSUE #77：着色与文案都走 robotDisplayState，**在线与否只认 is_online**。
  // 旧实现用 `status_code === 0` 判断，于是停报的机器人保持最后一次的颜色与温度 ——
  // 一台 1 小时前断电的"预警"机器人会一直亮着黄灯。现在它是灰色的「失联」。
  const state = robotDisplayState(robot.is_online === true, robot.status_code, hasEverReported(robot));
  const statusColor = DISPLAY_COLOR[state];
  const statusLabel = DISPLAY_LABEL[state];
  const offlineDisplay = isOfflineDisplay(state);
  const battColor = batteryColor(robot.battery_pct);
  const tempClr = offlineDisplay ? COLORS.statusOffline : tempColor(robot.max_joint_temp);
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
          style={{ ...FONT.cardId, color: statusColor === COLORS.statusOffline ? COLORS.textSecondary : COLORS.textPrimary }}
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
          {offlineDisplay ? '--' : `${robot.max_joint_temp.toFixed(0)}°C`}
        </span>

        {/* 5. 状态文字（ISSUE #77：掉线显示「失联」，不保留旧 status_code 的文案） */}
        <span className="mt-auto" style={{ ...FONT.metric, color: statusColor }}>
          {statusLabel}
        </span>
      </div>
    </button>
  );
});

/**
 * 100 卡网格本体。用 memo 包一层：父级 App 在绘制 Drawer（选中态变化）时，
 * 该子树 props 完全未变，memo 命中即可整块跳过 —— 不必为 100 张卡逐个 diff。
 * 网格自身只在 robots / selectedRobotId 引用变化时重渲染（即 100ms 刷库帧）。
 */
const RobotGrid = memo(function RobotGrid() {
  // 订单 16 T2：卡片墙渲染计数。
  //
  // 用 useRef 而不是 state —— 这是本计数能存在的前提：放 state 会 setState →
  // 触发本组件重渲染 → 计数又 +1 → 无限循环。useRef 是渲染期可安全写的可变盒子，
  // 写它不产生任何 React 订阅通知（不触发额外渲染，满足订单 16 的硬约束）。
  //
  // 计数点选在 **RobotGrid 本体**而不是 100 个 RobotCard：验收问的是
  // "卡片墙整块重渲染了几次"，那对应的是"store 通知本组件几次"。
  // 逐卡计数会被 memo 命中的卡混入（那些卡根本没重渲染），数字没有对照意义。
  //
  // ⚠️ React.StrictMode（main.tsx）在**开发模式**下会双调用组件函数，
  // 故 dev 下 cardRenders 约为真实值的 2 倍。生产构建（vite build）无此行为，
  // 对照 storeCommits 时应以生产数据为准；dev 下看两者"同比例"即可。
  const renderCountRef = useRef(0);
  renderCountRef.current += 1;
  countCardRender(renderCountRef.current);

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
});

export default RobotGrid;
