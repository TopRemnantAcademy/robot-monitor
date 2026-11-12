// RobotAvatar — cute on-palette SVG robot illustration for the fleet.
//
// User request (C): make the robots a "cute" type. Two form factors:
//   - 'humanoid' : HUMANOID-24DOF bipedal worker (the demo factory fleet) — chibi big-head style.
//   - 'delivery' : SEGWAY-S1 style delivery robot (the encosmart-seeded fleet) — round toaster-bot style.
//
// Design notes:
//   - Big shiny eyes, a small smile, and soft rosy cheeks are the cute staples.
//   - State coloring is preserved exactly: the soft halo, the antenna/sensor light, the chest/status
//     light, and the eye ring all derive from `stateColor(state)` (the theme-aware DISPLAY_COLOR /
//     accent tokens). No new status hue is introduced.
//   - Motions are GPU-friendly (transform / opacity only) and respect prefers-reduced-motion
//     (handled by theme/motion.css). `fx-float` gives the bot a gentle bob; the antenna blinks;
//     the chest/status light pulses. These are disabled for the 100-card wall (animated=false) for perf.

import { memo } from 'react';
import { COLORS, DISPLAY_COLOR, type RobotDisplayState } from '../theme/tokens';

export type RobotVariant = 'humanoid' | 'delivery';

type AvatarState = RobotDisplayState | 'online' | 'warning' | 'fault' | 'offline' | 'stale';

interface RobotAvatarProps {
  variant?: RobotVariant;
  state?: AvatarState;
  /** Pixel size of the square viewport. */
  size?: number;
  /** Enable gentle bob + glow + blink (kept off for the 100-card wall for perf). */
  animated?: boolean;
  glow?: boolean;
  className?: string;
}

/** Soft rosy blush — a fixed cute accent (not a status hue). */
const BLUSH = '#FF9FB2';
/** Cute dark eye/smile ink that reads on both themes. */
const INK = '#1B2230';
/** Big shiny eye white. */
const EYE_WHITE = '#FCFEFF';

/** Map any accepted state string to the theme-aware display color. */
function stateColor(state?: AvatarState): string {
  switch (state) {
    case 'online':
      return DISPLAY_COLOR.online;
    case 'warning':
      return DISPLAY_COLOR.warning;
    case 'fault':
      return DISPLAY_COLOR.fault;
    case 'stale':
    case 'offline':
      return DISPLAY_COLOR.offline;
    default:
      return COLORS.accent;
  }
}

/** Derive avatar variant from a robot_id (the telemetry contract has no model_type field). */
export function variantFromId(id: string | undefined): RobotVariant {
  if (!id) return 'humanoid';
  if (id.startsWith('humanoid')) return 'humanoid';
  return 'delivery'; // SEGWAY-S1 (S1RAM…), Deliee, etc.
}

/** Derive a display state from a robot card/telemetry object. */
export function stateFromRobot(robot: {
  is_online?: boolean;
  status_code?: number;
  last_seen?: number | null;
}): RobotDisplayState {
  const hasSeen = robot.last_seen !== null && robot.last_seen !== undefined;
  if (!robot.is_online) return hasSeen ? 'stale' : 'offline';
  if (robot.status_code === 3) return 'fault';
  if (robot.status_code === 2) return 'warning';
  return 'online';
}

/** Chibi humanoid: big round head, big shiny eyes, blush, little smile. */
function Humanoid({ color, animated }: { color: string; animated: boolean }) {
  return (
    <g className={animated ? 'fx-float' : undefined}>
      {/* Antenna */}
      <line x1="60" y1="20" x2="60" y2="11" stroke={COLORS.border} strokeWidth="2.4" strokeLinecap="round" />
      <circle
        cx="60"
        cy="9"
        r="3.6"
        fill={color}
        className={animated ? 'fx-blink' : undefined}
        style={animated ? { filter: `drop-shadow(0 0 5px ${color})` } : undefined}
      />
      {/* Big round head */}
      <circle cx="60" cy="46" r="27" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      {/* Rosy cheeks */}
      <ellipse cx="44" cy="53" rx="5.4" ry="3.6" fill={BLUSH} opacity="0.85" />
      <ellipse cx="76" cy="53" rx="5.4" ry="3.6" fill={BLUSH} opacity="0.85" />
      {/* Big shiny eyes (state-colored ring ties in status) */}
      <circle cx="50" cy="45" r="7.2" fill={EYE_WHITE} stroke={color} strokeWidth="1.8" />
      <circle cx="70" cy="45" r="7.2" fill={EYE_WHITE} stroke={color} strokeWidth="1.8" />
      <circle cx="50" cy="45" r="4.2" fill={INK} />
      <circle cx="70" cy="45" r="4.2" fill={INK} />
      <circle cx="51.6" cy="43.4" r="1.7" fill="#FFFFFF" />
      <circle cx="71.6" cy="43.4" r="1.7" fill="#FFFFFF" />
      {/* Little smile */}
      <path d="M52 56 Q60 63 68 56" fill="none" stroke={INK} strokeWidth="2.4" strokeLinecap="round" />
      {/* Tiny round body */}
      <rect x="44" y="74" width="32" height="30" rx="14" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      {/* Chest light (state color) */}
      <circle
        cx="60"
        cy="89"
        r="4.6"
        fill={color}
        className={animated ? 'fx-glow' : undefined}
        style={{ ['--fx-c' as string]: color }}
      />
      {/* Chubby arms */}
      <circle cx="39" cy="86" r="5.6" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      <circle cx="81" cy="86" r="5.6" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      {/* Little feet */}
      <ellipse cx="52" cy="106" rx="6" ry="4" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      <ellipse cx="68" cy="106" rx="6" ry="4" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
    </g>
  );
}

/** Round delivery bot: pill body, screen face, big eyes, blush, smile, wheels. */
function Delivery({ color, animated }: { color: string; animated: boolean }) {
  return (
    <g className={animated ? 'fx-float' : undefined}>
      {/* Top sensor pod */}
      <rect x="50" y="12" width="20" height="10" rx="4" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      <circle
        cx="60"
        cy="10"
        r="3.4"
        fill={color}
        className={animated ? 'fx-blink' : undefined}
        style={animated ? { filter: `drop-shadow(0 0 5px ${color})` } : undefined}
      />
      {/* Round body */}
      <rect x="34" y="24" width="52" height="64" rx="24" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      {/* Face screen */}
      <rect x="42" y="36" width="36" height="30" rx="14" fill="#10151F" />
      {/* Rosy cheeks */}
      <ellipse cx="47" cy="58" rx="3.8" ry="2.5" fill={BLUSH} opacity="0.9" />
      <ellipse cx="73" cy="58" rx="3.8" ry="2.5" fill={BLUSH} opacity="0.9" />
      {/* Big shiny eyes */}
      <circle cx="53" cy="50" r="5.6" fill={EYE_WHITE} />
      <circle cx="67" cy="50" r="5.6" fill={EYE_WHITE} />
      <circle cx="53" cy="50" r="3.2" fill={INK} />
      <circle cx="67" cy="50" r="3.2" fill={INK} />
      <circle cx="54.2" cy="48.6" r="1.3" fill="#FFFFFF" />
      <circle cx="68.2" cy="48.6" r="1.3" fill="#FFFFFF" />
      {/* Smile (light ink on the dark screen) */}
      <path d="M53 58 Q60 64 67 58" fill="none" stroke={EYE_WHITE} strokeWidth="2" strokeLinecap="round" />
      {/* Status strip (state color) */}
      <rect
        x="50"
        y="70"
        width="20"
        height="3.4"
        rx="1.7"
        fill={color}
        className={animated ? 'fx-glow' : undefined}
        style={{ ['--fx-c' as string]: color }}
      />
      {/* Chassis */}
      <rect x="30" y="84" width="60" height="16" rx="8" fill={COLORS.surface} stroke={COLORS.border} strokeWidth="2.4" />
      {/* Wheels */}
      <circle cx="42" cy="100" r="6" fill="#10151F" stroke={COLORS.border} strokeWidth="2.4" />
      <circle cx="78" cy="100" r="6" fill="#10151F" stroke={COLORS.border} strokeWidth="2.4" />
    </g>
  );
}

function RobotAvatarBase({
  variant = 'humanoid',
  state,
  size = 96,
  animated = true,
  glow = true,
  className,
}: RobotAvatarProps) {
  const color = stateColor(state);
  const cx = 60;
  const cy = 60;

  return (
    <svg
      viewBox="0 0 120 120"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={variant === 'humanoid' ? 'Cute humanoid robot' : 'Cute delivery robot'}
      style={{ display: 'block', overflow: 'visible' }}
    >
      {/* Soft glow halo behind the bot (palette color, translucent). */}
      <circle
        cx={cx}
        cy={cy}
        r="54"
        fill={color}
        opacity={glow ? 0.14 : 0.08}
        style={animated && glow ? { filter: `drop-shadow(0 0 10px ${color})` } : undefined}
      />

      {variant === 'humanoid' ? (
        <Humanoid color={color} animated={animated} />
      ) : (
        <Delivery color={color} animated={animated} />
      )}
    </svg>
  );
}

const RobotAvatar = memo(RobotAvatarBase);
export default RobotAvatar;
