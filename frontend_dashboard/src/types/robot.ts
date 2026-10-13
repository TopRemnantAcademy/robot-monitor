// spec/03 §7 类型定义（与 spec/00 §5.2 信封对齐）
//
// 【订单 18 · 契约即代码】REST 侧类型不再手写，改为**从后端 /openapi.json 生成**：
//   docs/contracts/openapi.snapshot.json  →  src/types/api.gen.ts（openapi-typescript）
//   本文件做**桥接（re-export）**，对外导出名一个都不改，组件无需改动。
//
// 为什么用桥接而不是直接改组件 import：
//   10 个文件从本模块 import 类型（`grep -rn "types/robot" src/`），
//   直接把 `RobotTelemetry` 换成 `components['schemas']['RobotTelemetryOut']`
//   会波及全部调用点，且生成名一旦随后端改名就会全线崩。
//   bridge 只在这一个文件里做名字映射，爆炸半径 = 1 个文件。
//
// 手写与生成的分工：
//   - **生成**：所有 REST 响应（`/robots`、`/robots/{id}/history`、`/healthz`）——
//     它们的字段由后端 response_model 决定，人手抄写必然漂移（本仓已真实翻车过一次）。
//   - **手写**：WS 信封（snapshot/telemetry/alert）—— WebSocket 不在 OpenAPI 描述范围内，
//     FastAPI **不会**把 WS 路由写进 /openapi.json。其机器可读契约见
//     `docs/contracts/ws-envelope.schema.json`，前端由 `src/lib/wsEnvelope.ts` 在
//     开发模式下用 ajv 校验。

import type { components } from './api.gen';

// --------------------------------------------------------------- 生成类型的快捷别名
//
// 直接 re-export 生成类型本身（而不是复制字段），保证「生成即事实」：
// 后端删一个字段 → 重新生成 → 这里立刻编译报错，不会静默留一个幽灵类型。
type Schemas = components['schemas'];

// --------------------------------------------------------------- 关节与状态（手写）

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

// --------------------------------------------------------------- REST 类型（由 openapi.json 生成）

/**
 * GET /robots 响应（生成自 `RobotListResponse`）。
 *
 * `Omit` 的理由（**实测踩坑，记录在此避免回退**）：OpenAPI 只能表达
 * `status_code` 是 `integer`，生成出来就是 `number`；而手写版是 `RobotStatus`
 * 的字面量联合 `0|1|2|3`，`theme/tokens.ts` 的 `STATUS_COLOR: Record<RobotStatus, string>`
 * 靠这个收窄做穷尽查找。放任 `number` 直通会让
 * `STATUS_COLOR[robot.status_code]` 报 TS7053（实测 4 处：FleetOverviewCards.tsx:17/63、
 * RobotDetailDrawer.tsx:159/185）—— 等于为了"自动生成"去改组件，恰好是本单禁止项。
 * 故此处只把 `status_code` 重新收窄回 `RobotStatus` 并**原样拼回其余字段**：
 * 字段集合仍由生成类型决定（多一个/少一个都会编译报错），收窄只影响精度。
 */
export type RobotTelemetry = Omit<Schemas['RobotTelemetryOut'], 'status_code'> & {
  status_code: RobotStatus;
};

/**
 * 「这台机器人现在还活着吗」—— **只信后端下发的 `is_online`**（ISSUE #77）。
 *
 * 为什么不能再用 `status_code === 0` 反推：
 *   `status_code` 是**机器人自述**的最后一帧状态。机器人停报后没人来改写它，
 *   它就冻在最后那个值上；于是断电一小时的机器人只要最后上报时是"正常"，
 *   大屏就把永久把它算作在线（ISSUE #77 的「在线 100 / 离线 0」假象）。
 *   `is_online` 来自**接收侧事实**（后端 `now - last_seen <= 阈值`，
 *   见 backend_ingestion/app/services/liveness.py），停报即不再前进。
 *
 * 老数据/老后端兼容：字段缺失时 `Boolean(undefined) === false` —— 保守判离线，
 * 不谎报在线。这与「宁可漏报在线」的取舍一致（谎报在线会掩盖真实掉线）。
 */
export function isRobotOnline(robot: Pick<RobotTelemetry, 'is_online'>): boolean {
  return robot.is_online === true;
}

/** GET /robots 响应（生成自 `RobotListResponse`）。 */
export type RobotListResponse = Omit<Schemas['RobotListResponse'], 'robots'> & {
  robots: RobotTelemetry[];
};

/**
 * GET /robots/{id}/history 的单个时间桶（生成自 `HistoryPointOut`）。
 *
 * 注意：`/history` **带 `status_code`**（后端 SQL 用 `last(status_code, time)` 取桶内最新值，
 * main.py:189），而手写版 HistoryPoint 此前**漏了这一列** ——
 * 也就是说"生成类型"第一次跑就发现了一处存量漂移。此处按后端为准补齐，
 * 不改前端任何调用点（Drawer 只用 time/joint_* 三键，多一个字段不影响）。
 */
export type HistoryPoint = Schemas['HistoryPointOut'];

/** GET /robots/{id}/history 响应（生成自 `RobotHistoryResponse`）。 */
export type RobotHistoryResponse = Schemas['RobotHistoryResponse'];

/** GET /healthz 响应（生成自 `HealthResponse`）。 */
export type HealthResponse = Schemas['HealthResponse'];

// --------------------------------------------------------------- WS 专属类型（手写）
//
// OpenAPI 管不到 WS，故以下四类保持手写；其结构与
// `docs/contracts/ws-envelope.schema.json` 一一对应（后者是机器可读的事实来源）。

/** 告警（spec/00 §5.2 ③）。字段与后端 WS `{"type":"alert","data":{...}}` 的 data 一致。 */
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
  /**
   * 是否被抑制（订单 35）。
   *
   * 恒为 false/undefined 就是**对**的：抑制判定发生在 alert_engine 的 `emit()` 里，
   * 被抑制的告警**不 PUBLISH** `robot:alerts`（那正是抑制的语义：不推给看板实时流）。
   * 这个字段留在 WS 类型里只为对齐 `AlertApiItem` 的行模型，值永远取自 REST。
   */
  suppressed?: boolean;
  /** 压制它的根因规则（订单 35）：offline / motor_temp / ... */
  suppressed_by?: string | null;
}

/** 告警筛选（订单 17 T4）：全部 / 未确认 / 已升级。
 *
 * 订单 35 说明：「已抑制」**刻意不做成第四个筛选项**。
 * 抑制的正交维度是「显示 / 不显示」而不是「看哪一类」——做成 tab 会让
 * 「未确认 ∪ 已抑制」这类组合无法表达。故它落在 `AlertLogTable` 的独立开关上
 * （见该组件的 `showSuppressed`），筛选 tab 仍是三选一。
 */
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
  /**
   * 是否被抑制（订单 35，来自 `GET /alerts?suppressed=…`）。
   *
   * 「已抑制」= 这条告警当时被同机更高 severity 的根因告警（或整机离线）压住，
   * 只落库、没出流。它**不是**"已经不用管了"——根因恢复后原告警会重新报出来，
   * 故前端只降视觉权重、不隐藏。
   */
  suppressed: boolean;
  /** 压制它的根因规则；`suppressed=false` 时为 null。 */
  suppressed_by: string | null;
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
