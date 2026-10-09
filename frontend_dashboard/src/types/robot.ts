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
