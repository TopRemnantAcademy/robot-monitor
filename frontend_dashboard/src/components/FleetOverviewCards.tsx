// spec/03 §5.1 —— 100-card wall
// grid-cols-5 md:grid-cols-10 gap-2, each card aspect-[4/3], all colors from theme/tokens.

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

/** Cards only consume their own robotId, combined with memo + self-subscription so "cards unchanged this frame" don't re-render */
interface RobotCardProps {
  robotId: string;
  selected: boolean;
  onOpen: (robotId: string) => void;
}

const RobotCard = memo(function RobotCard({ robotId, selected, onOpen }: RobotCardProps) {
  const { t } = useI18n();
  // ISSUE #75 structural sharing: the card **itself** subscribes to its own cell (`s.robots.get(robotId)`).
  // The old form had the grid subscribe to the whole Map → every store-flush frame rebuilt 100 card elements + 100 memo
  // comparisons (even when only 3 changed this frame); now zustand outputs per selector (Object.is) and judges per card —
  // only cards whose robot object reference changed re-render, the grid body itself re-renders zero times.
  // The Map holds 100 robots resident (offline placeholder fallback), but by type get can still return undefined,
  // defensive short-circuit avoids white-screening when the upstream contract changes.
  const robot = useRobotStore((s) => s.robots.get(robotId));
  if (!robot) return null;

  // ISSUE #77: coloring and text both go through robotDisplayState, **online-ness only recognizes is_online**.
  // The old implementation used `status_code === 0` to judge, so a robot that stopped reporting kept its last color and temperature ——
  // a "warning" robot that lost power an hour ago would keep glowing yellow. Now it's the gray "lost contact".
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
      {/* 1. Top status bar */}
      <div className="h-1 w-full shrink-0" style={{ backgroundColor: statusColor }} />

      <div className="flex flex-col flex-1 min-h-0 px-2 py-1 gap-0.5">
        {/* 2. Robot ID (H-042 short name, 13px/600) */}
        <span
          className="truncate"
          style={{ ...FONT.cardId, color: statusColor === COLORS.statusOffline ? COLORS.textSecondary : COLORS.textPrimary }}
        >
          H-{robot.robot_id.replace(/^humanoid-/, '')}
        </span>

        {/* 3. Battery bar (width = battery %) */}
        <div className="flex items-center gap-1">
          <div className="flex-1 h-1.5 rounded-sm overflow-hidden" style={{ backgroundColor: COLORS.border }}>
            <div className="h-full" style={{ width: `${battWidth}%`, backgroundColor: battColor }} />
          </div>
          <span className="tabular-nums shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            {robot.battery_pct.toFixed(0)}%
          </span>
        </div>

        {/* 4. Max joint temperature (16px/700, colored by temp zone) */}
        <span className="tabular-nums" style={{ fontSize: '16px', fontWeight: 700, color: tempClr }}>
          {offlineDisplay ? '--' : `${robot.max_joint_temp.toFixed(0)}°C`}
        </span>

        {/* 5. Status text (ISSUE #77: offline shows "lost contact", don't keep the old status_code text) */}
        <span className="mt-auto" style={{ ...FONT.metric, color: statusColor }}>
          {statusLabel}
        </span>
      </div>
    </button>
  );
});

/**
 * 100-card grid body. Wrapped in one memo: when the parent App draws the Drawer (selected-state change),
 * this subtree's props are completely unchanged, memo hit skips the whole block —— no need to diff 100 cards individually.
 *
 * ISSUE #75 structural sharing (consumer side): the grid **no longer subscribes to the whole robots Map**.
 * The old form `useRobotStore((s) => s.robots)` made the grid body re-render every 100ms store-flush frame
 * and rebuild 100 card elements; now it subscribes to a constant ID list (`buildRobotIds(100)`,
 * content never changes → this component re-renders zero times except on selectedRobotId change), each card
 * fetches data via its own `s.robots.get(robotId)` self-subscription —— only cards with reference changes re-render.
 */
const RobotGrid = memo(function RobotGrid() {
  // Order 16 T2: card-wall render counter.
  //
  // Uses useRef instead of state —— this is the premise that this counter can exist: putting it in state would setState →
  // trigger this component to re-render → counter +1 again → infinite loop. useRef is a mutable box safe to write during render,
  // writing to it produces no React subscription notification (no extra render, satisfying Order 16's hard constraint).
  //
  // The counting point is on the **RobotGrid body** rather than the 100 RobotCards: after #75 the grid body
  // only renders on selected-state change, per-card re-renders no longer raise this number —— "wallRenders doesn't grow with store-flush
  // frames" is itself the acceptance signal of #75 structural sharing. In dev mode StrictMode double-invoke
  // still makes the reading ~2x the real value; compare against the production build.
  const renderCountRef = useRef(0);
  renderCountRef.current += 1;
  countCardRender(renderCountRef.current);

  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const selectRobot = useRobotStore((s) => s.selectRobot);

  // Constant list: content and reference both never change, useMemo is merely a contractual expression (hoping for compiler constant folding).
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
