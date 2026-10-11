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

/**
 * 大屏展示态：online / warning / fault / stale / offline（ISSUE #77）。
 *
 * 为什么不直接用 `status_code`：`status_code` 是**机器人自述**的最后一帧状态，
 * 它不回答"这台机器现在还活着吗"。后端下发的 `is_online` 才回答。
 * 于是卡片上有两个正交的维度：**还在不在**（is_online）× **状态好不好**（status_code）。
 * 这个枚举把两维压成一个可着色的值，且**不让 status_code 覆盖离线结论** ——
 * 一台停报的"预警"机器人必须显示「失联」，而不是继续保持黄色预警。
 *
 * stale 与 offline 的区别（两者同色但不同事）：
 *   stale   —— 收到过、现在停了：**该去现场**；
 *   offline —— 从未收到过：init.sql 预置的空位，开机即可消。
 * 两者都不在线，但处置动作完全不同，故不合并。
 */
export type RobotDisplayState = 'online' | 'warning' | 'fault' | 'stale' | 'offline';

/** 展示态 → 色。stale / offline 复用 statusOffline 灰：不新造色相（spec/03 §3）。 */
export const DISPLAY_COLOR: Record<RobotDisplayState, string> = {
  online: COLORS.statusNormal,
  warning: COLORS.statusWarning,
  fault: COLORS.statusFault,
  stale: COLORS.statusOffline,
  offline: COLORS.statusOffline,
};

/** 展示态 → 文案。`stale` → 「失联」（而不是「离线」），见 RobotDisplayState 注释。 */
export const DISPLAY_LABEL: Record<RobotDisplayState, string> = {
  online: '正常',
  warning: '预警',
  fault: '故障',
  stale: '失联',
  offline: '离线',
};

/** 展示态是否代表「后端已判定不在线」（stale / offline 两种）。 */
export function isOfflineDisplay(state: RobotDisplayState): boolean {
  return state === 'stale' || state === 'offline';
}

/**
 * 由「后端在线标志 + 最后一帧状态码 + 是否有过数据」推出展示态。
 *
 * @param isOnline 后端下发的在线标志（ISSUE #77；唯一权威）
 * @param statusCode 最后一帧自述状态（仅在 isOnline 为真时参与着色）
 * @param hasSeen 是否收到过遥测（区分「失联」与「从未上报」）
 */
export function robotDisplayState(
  isOnline: boolean,
  statusCode: RobotStatus,
  hasSeen: boolean,
): RobotDisplayState {
  if (!isOnline) return hasSeen ? 'stale' : 'offline';
  // 在线时 status_code 才有意义（它是"此刻状态好不好"），离线时它只是最后一帧的残影。
  if (statusCode === 3) return 'fault';
  if (statusCode === 2) return 'warning';
  return 'online';
}

/**
 * 是否**收到过**该机器人遥测。
 *
 * `last_seen` 由后端下发（ISSUE #77），`null` = 从未上报。
 * 老后端不给该字段时回落为「从未上报」——保守，且与 `offlinePlaceholder` 语义一致。
 */
export function hasEverReported(robot: { last_seen?: number | null }): boolean {
  return robot.last_seen !== null && robot.last_seen !== undefined;
}

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
