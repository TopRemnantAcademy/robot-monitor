// spec/03 §3 / orders/05「设计 token」—— 严格照抄，禁止自改色。
// 组件里需要按数值计算颜色（电量条、温度色块）时统一从这里取，避免散落魔法字符串。

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
  /** Drawer 遮罩：#0B0E14 的 55% 透明度（由 bgBase 派生，不引入新色相） */
  overlay: 'rgba(11, 14, 20, 0.55)',
} as const;

/** 字号/圆角 token（spec/03 §3）。需要内联 style 时引用此处。 */
export const FONT = {
  cardId: { fontSize: '13px', fontWeight: 600 },
  metric: { fontSize: '11px', fontWeight: 400 },
  title: { fontSize: '18px', fontWeight: 700 },
} as const;

export const RADIUS = {
  card: '10px',
  drawer: '0px',
} as const;

/** status_code → 状态色（0 离线 / 1 正常 / 2 预警 / 3 故障） */
export const STATUS_COLOR: Record<RobotStatus, string> = {
  0: COLORS.statusOffline,
  1: COLORS.statusNormal,
  2: COLORS.statusWarning,
  3: COLORS.statusFault,
};

export const STATUS_LABEL: Record<RobotStatus, string> = {
  0: '离线',
  1: '正常',
  2: '预警',
  3: '故障',
};

/** 温度色阶：0~50 绿 / 50~70 黄 / >70 红（spec/03 §5.1 第 4 条、§5.4） */
export function tempColor(temp: number): string {
  if (temp > 70) return COLORS.statusFault;
  if (temp >= 50) return COLORS.statusWarning;
  return COLORS.statusNormal;
}

/** 电量色阶：>50 绿 / 15~50 黄 / <15 红（spec/03 §5.1 第 3 条） */
export function batteryColor(pct: number): string {
  if (pct < 15) return COLORS.statusFault;
  if (pct <= 50) return COLORS.statusWarning;
  return COLORS.statusNormal;
}

/** 告警 severity → 色（沿用状态色，避免自创配色） */
export function severityColor(severity: number): string {
  if (severity >= 3) return COLORS.statusFault;
  if (severity === 2) return COLORS.statusWarning;
  return COLORS.accent;
}
