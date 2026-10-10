// spec/03 §7 类型定义（与 spec/00 §5.2 信封对齐）
// 字段名逐字对齐后端 schemas/telemetry.py 与 ORM 模型，禁止改名。

/** 0 离线 / 1 正常 / 2 预警 / 3 故障（spec/00 §4.1 status_code 注释） */
export type RobotStatus = 0 | 1 | 2 | 3;

/** 24 关节标准键名，唯一事实来源 spec/00 §5.5 */
export const JOINT_NAMES = [
  'left_shoulder_pitch', 'left_shoulder_roll', 'left_shoulder_yaw', 'left_elbow_pitch',
  'left_wrist_pitch', 'left_wrist_roll',
  'right_shoulder_pitch', 'right_shoulder_roll', 'right_shoulder_yaw', 'right_elbow_pitch',
  'right_wrist_pitch', 'right_wrist_roll',
  'left_hip_yaw', 'left_hip_roll', 'left_hip_pitch',
  'left_knee_pitch', 'left_ankle_pitch', 'left_ankle_roll',
  'right_hip_yaw', 'right_hip_roll', 'right_hip_pitch',
  'right_knee_pitch', 'right_ankle_pitch', 'right_ankle_roll',
] as const;

export type JointName = (typeof JOINT_NAMES)[number];

export type JointDict = Record<string, number>;

export interface RobotTelemetry {
  robot_id: string;
  status_code: RobotStatus;
  battery_pct: number;
  max_joint_temp: number;
  joint_temperatures: JointDict;
  joint_torques: JointDict;
  joint_positions: JointDict;
  error_code: number;
  ts: number;
}

export interface RobotAlert {
  robot_id: string;
  rule_id: string;
  severity: number;
  message: string;
  ts: number;
  /**
   * 告警主键（订单 17）。WS 初发信封由 alert_engine/app/store.py 的
   * `INSERT ... RETURNING alert_id` 带回；DB 故障时后端会发 null，
   * 此时本条告警无法寻址（确认按钮应禁用）。
   */
  alert_id?: number | null;
  /** 是否已升级重发（订单 17）：true 时前端显著色 + 「⚠ 已升级」。 */
  escalated?: boolean;
  /** 是否已确认（订单 17）：来自 GET /alerts 或本地 ack 后的乐观更新。 */
  ack?: boolean;
  ack_at?: string | null;
  ack_by?: string | null;
}

/** 告警筛选（订单 17 T4）：全部 / 未确认 / 已升级。 */
export type AlertFilter = 'all' | 'unacked' | 'escalated';

/** `GET /alerts` 单条（与后端 app/alerts/schemas.py:AlertItem 逐字段对齐）。 */
export interface AlertApiItem {
  alert_id: number;
  time: string;
  robot_id: string;
  rule_id: string;
  severity: number;
  message: string;
  detail: Record<string, unknown> | null;
  ack: boolean;
  ack_at: string | null;
  ack_by: string | null;
  escalated: boolean;
}

/** `GET /alerts` 分页响应（app/alerts/schemas.py:AlertPage）。 */
export interface AlertPage {
  items: AlertApiItem[];
  total: number;
  limit: number;
  offset: number;
}

/** `POST /alerts/{id}/ack|unack` 响应（app/alerts/schemas.py:AckResult）。 */
export interface AckResult {
  alert_id: number;
  ack: boolean;
  ack_at: string | null;
  ack_by: string | null;
  /** false = 幂等命中（已是目标状态），不是错误。 */
  changed: boolean;
}

/** GET /robots/{id}/history 单点（spec/00 §5.3） */
export interface HistoryPoint {
  time: string;
  battery_pct: number;
  max_joint_temp: number;
  joint_temperatures: JointDict;
  joint_torques: JointDict;
  joint_positions?: JointDict;
}

export interface RobotHistoryResponse {
  robot_id: string;
  points: HistoryPoint[];
}

/** ws://{host}:8000/ws/robots 统一信封（spec/00 §5.2） */
export interface SnapshotEnvelope {
  type: 'snapshot';
  data: { robots: RobotTelemetry[] };
}

export interface TelemetryEnvelope {
  type: 'telemetry';
  data: RobotTelemetry;
}

export interface AlertEnvelope {
  type: 'alert';
  data: RobotAlert;
}

export type WsEnvelope = SnapshotEnvelope | TelemetryEnvelope | AlertEnvelope;

export type WsStatus = 'connecting' | 'open' | 'closed';
