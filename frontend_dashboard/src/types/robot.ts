// spec/03 §7 type definitions (aligned with the spec/00 §5.2 envelope)
//
// ORDER 18 · CONTRACT AS CODE: REST-side types are no longer hand-written, but **generated from the backend /openapi.json**:
//   docs/contracts/openapi.snapshot.json  →  src/types/api.gen.ts (openapi-typescript)
//   This file does a **bridge (re-export)**, keeping every exported name unchanged so components need no change.
//
// Why a bridge instead of changing component imports directly:
//   10 files import types from this module (`grep -rn "types/robot" src/`),
//   replacing `RobotTelemetry` with `components['schemas']['RobotTelemetryOut']`
//   would touch every call site, and if a generated name later changes with the backend the whole line breaks.
//   The bridge does the name mapping in just this one file; blast radius = 1 file.
//
// Division of labor between hand-written and generated:
//   - **Generated**: all REST responses (`/robots`, `/robots/{id}/history`, `/healthz`) ——
//     their fields are decided by the backend response_model; hand-copying inevitably drifts (this repo already truly crashed once).
//   - **Hand-written**: the WS envelope (snapshot/telemetry/alert) —— WebSocket is outside OpenAPI's description scope,
//     FastAPI **will not** write WS routes into /openapi.json. Its machine-readable contract is
//     `docs/contracts/ws-envelope.schema.json`, which the frontend validates with ajv in
//     dev mode via `src/lib/wsEnvelope.ts`.
import type { components } from './api.gen';

// --------------------------------------------------------------- Short aliases for generated types
//
// Directly re-export the generated types themselves (rather than copying fields), guaranteeing "generated = truth":
// backend removes a field → regenerate → this immediately fails to compile, instead of silently leaving a ghost type.
type Schemas = components['schemas'];

// --------------------------------------------------------------- Joints and status (hand-written)

/** 0 offline / 1 normal / 2 warning / 3 fault (spec/00 §4.1 status_code comment). */
export type RobotStatus = 0 | 1 | 2 | 3;

/** 24 standard joint key names, sole source of truth spec/00 §5.5. */
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

// --------------------------------------------------------------- REST types (generated from openapi.json)

/**
 * GET /robots response (generated from `RobotListResponse`).
 *
 * Reason for `Omit` (**measured pitfall, recorded here to avoid reverting**): OpenAPI can only express
 * `status_code` as `integer`, so it generates `number`; but the hand-written version is the `RobotStatus`
 * literal union `0|1|2|3`, which `theme/tokens.ts`'s `STATUS_COLOR: Record<RobotStatus, string>`
 * relies on for exhaustive lookup. Letting `number` pass through raw would make
 * `STATUS_COLOR[robot.status_code]` raise TS7053 (measured at 4 sites: FleetOverviewCards.tsx:17/63,
 * RobotDetailDrawer.tsx:159/185) —— i.e. changing components for the sake of "auto-generation", exactly this issue's forbidden item.
 * So here we only narrow `status_code` back to `RobotStatus` and **reassemble the rest of the fields verbatim**:
 * the field set is still decided by the generated type (one extra/missing would fail to compile); narrowing only affects precision.
 */
export type RobotTelemetry = Omit<Schemas['RobotTelemetryOut'], 'status_code'> & {
  status_code: RobotStatus;
};

/**
 * "Is this robot still alive right now" —— **only trust the backend-issued `is_online`** (ISSUE #77).
 *
 * Why we can no longer infer from `status_code === 0`:
 *   `status_code` is the robot's **self-reported** last frame state. After a robot stops reporting nobody rewrites it,
 *   so it freezes at that last value; thus a robot powered off for an hour, as long as its last report was "normal",
 *   would be permanently counted as online by the dashboard (ISSUE #77's "online 100 / offline 0" illusion).
 *   `is_online` comes from the **receiving-side fact** (backend `now - last_seen <= threshold`,
 *   see backend_ingestion/app/services/liveness.py); once reporting stops it stops advancing.
 *
 * Legacy data/backend compatibility: when a field is missing, `Boolean(undefined) === false` —— conservatively judge offline,
 * never falsely report online. This matches the "rather miss an online report than falsely report online" trade-off
 * (falsely reporting online would hide a real dropout).
 */
export function isRobotOnline(robot: Pick<RobotTelemetry, 'is_online'>): boolean {
  return robot.is_online === true;
}

/** GET /robots response (generated from `RobotListResponse`). */
export type RobotListResponse = Omit<Schemas['RobotListResponse'], 'robots'> & {
  robots: RobotTelemetry[];
};

/**
 * Single time bucket of GET /robots/{id}/history (generated from `HistoryPointOut`).
 *
 * Note: `/history` **includes `status_code`** (the backend SQL uses `last(status_code, time)` for the bucket's latest value,
 * main.py:189), whereas the hand-written HistoryPoint previously **missed this column** ——
 * i.e. the first run of "generated types" already surfaced one piece of existing drift. Fill it in per the backend here,
 * without changing any front-end call site (the Drawer only uses time/joint_* keys, an extra field doesn't matter).
 */
export type HistoryPoint = Schemas['HistoryPointOut'];

/** GET /robots/{id}/history response (generated from `RobotHistoryResponse`). */
export type RobotHistoryResponse = Schemas['RobotHistoryResponse'];

/** GET /healthz response (generated from `HealthResponse`). */
export type HealthResponse = Schemas['HealthResponse'];

// --------------------------------------------------------------- /supported-robots
//
// ⚠️ Hand-written (not openapi-generated): this endpoint was added 2026-11-09, and
// `docs/contracts/openapi.snapshot.json` has not been regenerated yet. Fields are verbatim-aligned with the backend
// `app/schemas/api.py:SupportedRobotOut` / `SupportedRobotsResponse`.
// **After the next run of `scripts/export-openapi.py` + `frontend_dashboard/scripts/gen-types.mjs`,
// it should be switched to generating from `api.gen.ts`'s `components['schemas']['SupportedRobotOut']`.**
/** GET /supported-robots single model (mirrors backend `SupportedRobotOut`). */
export interface SupportedRobot {
  model_type: string;
  display_name: string;
  form_factor: string;
  dof: number;
  vendor: string;
  description: string;
  /** true = catalog metadata unregistered, backend falls back to a placeholder (no fabricated display info). */
  generic: boolean;
  /** Total robots registered for this model. */
  total: number;
  /** Of those, currently online count (is_online). */
  online: number;
  /** Deduplicated list of registered firmware versions. */
  firmware_versions: string[];
  /** Deduplicated count of assigned stations. */
  stations: number;
}

/** GET /supported-robots response (mirrors backend `SupportedRobotsResponse`). */
export interface SupportedRobotsResponse {
  models: SupportedRobot[];
  total_robots: number;
}

/** react-query key (shared with SupportedRobotsPanel). */
export const supportedRobotsKey = ['supported-robots'] as const;

// --------------------------------------------------------------- WS-only types (hand-written)
//
// OpenAPI can't reach WS, so the following four types stay hand-written; their structure is
// one-to-one with `docs/contracts/ws-envelope.schema.json` (the latter is the machine-readable source of truth).
/** Alert (spec/00 §5.2 ③). Fields match the data of the backend WS `{"type":"alert","data":{...}}`. */
export interface RobotAlert {
  robot_id: string;
  rule_id: string;
  severity: number;
  message: string;
  ts: number;
  /**
   * Alert primary key (Order 17). The WS initial envelope carries it back from alert_engine/app/store.py's
   * `INSERT ... RETURNING alert_id`; on DB failure the backend sends null,
   * in which case this alert can't be addressed (the ack button should be disabled).
   */
  alert_id?: number | null;
  /** Whether it was escalated and re-sent (Order 17): true shows a prominent color + "⚠ Escalated". */
  escalated?: boolean;
  /** Whether it was acknowledged (Order 17): from GET /alerts or the optimistic update after a local ack. */
  ack?: boolean;
  ack_at?: string | null;
  ack_by?: string | null;
  /**
   * Whether it is suppressed (Order 35).
   *
   * It is **correct** for this to always be false/undefined: suppression is decided inside alert_engine's `emit()`,
   * and suppressed alerts are **not PUBLISH**ed to `robot:alerts` (that is exactly the suppression semantics: not pushed to the live board stream).
   * This field stays in the WS type only to align with `AlertApiItem`'s row model; its value always comes from REST.
   */
  suppressed?: boolean;
  /** The root-cause rule that suppresses it (Order 35): offline / motor_temp / ... */
  suppressed_by?: string | null;
}

/** Alert filter (Order 17 T4): all / unacknowledged / escalated.
 *
 * Order 35 note: "suppressed" is **deliberately not made a fourth filter tab**.
 * Suppression's orthogonal dimension is "show / don't show" rather than "which category" —— making it a tab
 * would make combinations like "unacknowledged ∪ suppressed" unexpressible. So it lands on `AlertLogTable`'s
 * independent toggle (see that component's `showSuppressed`); the filter tabs stay three-way.
 */
export type AlertFilter = 'all' | 'unacked' | 'escalated';

/** `GET /alerts` single item (field-by-field alignment with backend app/alerts/schemas.py:AlertItem). */
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
   * Whether it is suppressed (Order 35, from `GET /alerts?suppressed=…`).
   *
   * "Suppressed" = this alert was at the time pressed down by a higher-severity root-cause alert on the same machine
   * (or whole-machine offline); it only landed in the DB, not in the stream. It is **not** "no longer needs handling" ——
   * once the root cause recovers the original alert will be reported again, so the frontend only reduces visual weight, not hides it.
   */
  suppressed: boolean;
  /** The root-cause rule that suppresses it; null when `suppressed=false`. */
  suppressed_by: string | null;
}

/** `GET /alerts` paginated response (app/alerts/schemas.py:AlertPage). */
export interface AlertPage {
  items: AlertApiItem[];
  total: number;
  limit: number;
  offset: number;
}

/** `POST /alerts/{id}/ack|unack` response (app/alerts/schemas.py:AckResult). */
export interface AckResult {
  alert_id: number;
  ack: boolean;
  ack_at: string | null;
  ack_by: string | null;
  /** false = idempotent hit (already at the target state), not an error. */
  changed: boolean;
}

/** ws://{host}:8000/ws/robots unified envelope (spec/00 §5.2). */
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
