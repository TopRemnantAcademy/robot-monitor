// spec/03 §5.1 —— 100 卡片墙
// grid-cols-5 md:grid-cols-10 gap-2，每卡 aspect-[4/3]，颜色全部走 theme/tokens。

import { memo, useMemo, useRef } from 'react';
import {
  COLORS,
  DISPLAY_COLOR,
  FONT,
  batteryColor,
  hasEverReported,
  isOfflineDisplay,
  robotDisplayState,
  tempColor,
} from '../theme/tokens';
import { useRobotStore, buildRobotIds } from '../store/robotStore';
import { countCardRender } from '../lib/observability';
import { useI18n } from '../i18n/I18nProvider';

/** 卡片只吃自己的 robotId，配合 memo + 自订阅让「本帧没变的卡」不重渲染 */
interface RobotCardProps {
  robotId: string;
  selected: boolean;
  onOpen: (robotId: string) => void;
}

const RobotCard = memo(function RobotCard({ robotId, selected, onOpen }: RobotCardProps) {
  const { t } = useI18n();
  // ISSUE #75 结构性共享：卡片**自己**订阅自己那一格（`s.robots.get(robotId)`）。
  // 旧形态是网格订阅整张 Map → 每个刷库帧都重建 100 个卡片元素 + 100 次 memo
  // 比较（哪怕本帧只有 3 台变了）；现在 zustand 按选择器输出（Object.is）逐卡
  // 判定 —— 只有 robot 对象引用变化的卡才重渲染，网格本体零重渲染。
  // Map 里 100 台常驻（离线占位兜底），但类型上 get 仍可返回 undefined，
  // 防御性短路避免上游契约变化时白屏。
  const robot = useRobotStore((s) => s.robots.get(robotId));
  if (!robot) return null;

  // ISSUE #77：着色与文案都走 robotDisplayState，**在线与否只认 is_online**。
  // 旧实现用 `status_code === 0` 判断，于是停报的机器人保持最后一次的颜色与温度 ——
  // 一台 1 小时前断电的"预警"机器人会一直亮着黄灯。现在它是灰色的「失联」。
  const state = robotDisplayState(robot.is_online === true, robot.status_code, hasEverReported(robot));
  const statusColor = DISPLAY_COLOR[state];
  const statusLabel = t(`display.${state}`);
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
 *
 * ISSUE #75 结构性共享（消费侧）：网格**不再订阅整张 robots Map**。
 * 旧形态 `useRobotStore((s) => s.robots)` 让网格本体每个 100ms 刷库帧都重渲染
 * 并重建 100 个卡片元素；现在改为订阅常量 ID 列表（`buildRobotIds(100)`，
 * 内容永不变 → 本组件除 selectedRobotId 变化外**零重渲染**），每张卡通过
 * 自己的 `s.robots.get(robotId)` 自订阅取数 —— 只有引用变化的卡才重渲染。
 */
const RobotGrid = memo(function RobotGrid() {
  // 订单 16 T2：卡片墙渲染计数。
  //
  // 用 useRef 而不是 state —— 这是本计数能存在的前提：放 state 会 setState →
  // 触发本组件重渲染 → 计数又 +1 → 无限循环。useRef 是渲染期可安全写的可变盒子，
  // 写它不产生任何 React 订阅通知（不触发额外渲染，满足订单 16 的硬约束）。
  //
  // 计数点选在 **RobotGrid 本体**而不是 100 个 RobotCard：#75 之后网格本体
  // 只在选中态变化时渲染，逐卡重渲染不再抬升此数字 —— "wallRenders 不随刷库
  // 帧增长" 本身就是 #75 结构性共享的验收信号。dev 模式下 StrictMode 双调用
  // 仍使读数约为真实值 2 倍，对照以生产构建为准。
  const renderCountRef = useRef(0);
  renderCountRef.current += 1;
  countCardRender(renderCountRef.current);

  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  // 常量列表：内容与引用都永不变，useMemo 只是契约性表达（期待编译器常量折叠）。
  const ids = useMemo(() => buildRobotIds(100), []);

  return (
    <div className="flex-1 min-h-0 overflow-y-auto px-3 py-2">
      <div className="grid grid-cols-5 md:grid-cols-10 gap-2">
        {ids.map((id) => (
          <RobotCard key={id} robotId={id} selected={id === selectedRobotId} onOpen={selectRobot} />
        ))}
      </div>
    </div>
  );
});

export default RobotGrid;
