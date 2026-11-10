// spec/03 §3 / orders/05 "design tokens" — copy strictly, do not change colors yourself.
// When components need value-based colors (battery bar, temperature blocks), take them from here to avoid scattered magic strings.

import type { RobotStatus } from '../types/robot';

export const COLORS = {
  bgBase: '#0B0E14',
  surface: '#121722',
  border: '#1A2230',
  textPrimary: '#E6EDF3',
  textSecondary: '#8B98A9',
  accent: '#3B82F6',
  statusNormal: '#2ECC71',
  statusWarning: '#F1C40F',
  statusFault: '#FF4D4F',
  statusOffline: '#5A6472',
  /** Drawer overlay: #0B0E14 at 55% opacity (derived from bgBase, no new hue). */
  overlay: 'rgba(11, 14, 20, 0.55)',
} as const;

/** Font-size / radius tokens (spec/03 §3). Reference here when an inline style is needed. */
export const FONT = {
  cardId: { fontSize: '13px', fontWeight: 600 },
  metric: { fontSize: '11px', fontWeight: 400 },
  title: { fontSize: '18px', fontWeight: 700 },
} as const;

export const RADIUS = {
  card: '10px',
  drawer: '0px',
} as const;

/** status_code → state color (0 offline / 1 normal / 2 warning / 3 fault). */
export const STATUS_COLOR: Record<RobotStatus, string> = {
  0: COLORS.statusOffline,
  1: COLORS.statusNormal,
  2: COLORS.statusWarning,
  3: COLORS.statusFault,
};

export const STATUS_LABEL: Record<RobotStatus, string> = {
  0: 'offline',
  1: 'normal',
  2: 'warning',
  3: 'fault',
};

/**
 * Big-screen display state: online / warning / fault / stale / offline (ISSUE #77).
 *
 * Why not use `status_code` directly: `status_code` is the robot's own last reported frame state,
 * it does not answer "is this machine still alive right now". The backend's `is_online` answers that.
 * So a card has two orthogonal dimensions: **still present** (is_online) × **state good or not** (status_code).
 * This enum collapses the two into one colorable value, and **never lets status_code override the offline conclusion** ——
 * a "warning" robot that stopped reporting must show "lost", not stay yellow warning.
 *
 * Difference between stale and offline (same color, different meaning):
 *   stale   —— received before, now stopped: **go on-site**;
 *   offline —— never received: an empty slot pre-seeded by init.sql, cleared once powered on.
 * Both are offline, but the handling action is completely different, so they are not merged.
 */
export type RobotDisplayState = 'online' | 'warning' | 'fault' | 'stale' | 'offline';

/** Display state → color. stale / offline reuse statusOffline gray: no new hue (spec/03 §3). */
export const DISPLAY_COLOR: Record<RobotDisplayState, string> = {
  online: COLORS.statusNormal,
  warning: COLORS.statusWarning,
  fault: COLORS.statusFault,
  stale: COLORS.statusOffline,
  offline: COLORS.statusOffline,
};

/** Display state → text. `stale` → "lost" (not "offline"); see RobotDisplayState comment. */
export const DISPLAY_LABEL: Record<RobotDisplayState, string> = {
  online: 'normal',
  warning: 'warning',
  fault: 'fault',
  stale: 'lost',
  offline: 'offline',
};

/** Whether the display state means "backend judged it offline" (the two kinds: stale / offline). */
export function isOfflineDisplay(state: RobotDisplayState): boolean {
  return state === 'stale' || state === 'offline';
}

/**
 * Derive the display state from "backend online flag + last frame status code + whether telemetry was ever seen".
 *
 * @param isOnline the backend's online flag (ISSUE #77; sole authority)
 * @param statusCode the last self-reported status (only affects coloring when isOnline is true)
 * @param hasSeen whether telemetry was ever received (distinguishes "lost" from "never reported")
 */
export function robotDisplayState(
  isOnline: boolean,
  statusCode: RobotStatus,
  hasSeen: boolean,
): RobotDisplayState {
  if (!isOnline) return hasSeen ? 'stale' : 'offline';
  // status_code only matters when online (it means "how good is the state right now"); offline it is just the last frame's afterimage.
  if (statusCode === 3) return 'fault';
  if (statusCode === 2) return 'warning';
  return 'online';
}

/**
 * Whether telemetry for this robot was **ever received**.
 *
 * `last_seen` is sent by the backend (ISSUE #77), `null` = never reported.
 * When an old backend omits this field, fall back to "never reported" — conservative, and consistent with `offlinePlaceholder` semantics.
 */
export function hasEverReported(robot: { last_seen?: number | null }): boolean {
  return robot.last_seen !== null && robot.last_seen !== undefined;
}

/** Temperature color scale: 0~50 green / 50~70 yellow / >70 red (spec/03 §5.1 item 4, §5.4). */
export function tempColor(temp: number): string {
  if (temp > 70) return COLORS.statusFault;
  if (temp >= 50) return COLORS.statusWarning;
  return COLORS.statusNormal;
}

/** Battery color scale: >50 green / 15~50 yellow / <15 red (spec/03 §5.1 item 3). */
export function batteryColor(pct: number): string {
  if (pct < 15) return COLORS.statusFault;
  if (pct <= 50) return COLORS.statusWarning;
  return COLORS.statusNormal;
}

/** Alert severity → color (reuses state colors to avoid inventing a palette). */
export function severityColor(severity: number): string {
  if (severity >= 3) return COLORS.statusFault;
  if (severity === 2) return COLORS.statusWarning;
  return COLORS.accent;
}
