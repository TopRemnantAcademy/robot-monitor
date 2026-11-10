// Zustand global state tree (spec/03 §1 / §5 / §6)
//
// Key performance constraint (AGENTS.md §3 performance guardrail):
// This store only exposes the batch entry point applyTelemetryBatch, called once per frame by the
// 100ms setInterval in useRobotWebSocket. 100 robots × 10Hz = 1000 msg/s; if every message did set()
// React would receive 1000 subscription notifications per second and re-render the main thread to death.
// So WS messages are first buffered into a useRef<Map>, then flushed here uniformly at 10Hz.

import { create } from 'zustand';
import type { RobotAlert, RobotStatus, RobotTelemetry } from '../types/robot';
import { JOINT_NAMES, isRobotOnline } from '../types/robot';

/** Alert-stream rolling cap, to prevent unbounded memory growth during long runs (~6 rows visible at 200px height). */
export const ALERT_BUFFER_LIMIT = 200;

/**
 * Fleet time-series sample point (for the UI trend line, see FleetHealthAnalytics).
 *
 * Why not compute the trend directly from WS frames: the robots Map is the **current snapshot**, with no history;
 * and /history only queries a single robot. Fleet-level trends (online rate / active alerts over time) need a rolling
 * time series, so we accumulate it on the client via low-frequency sampling (one point per 2s) —— fully decoupled
 * from the 100ms flush hot path; it only does one O(n) pass at sample time.
 */
export interface FleetSample {
  t: number;
  /** Online rate 0–100 (kept to 1 decimal). */
  onlineRate: number;
  /** Number of active alerts with severity ≥ 2. */
  activeAlerts: number;
  /** Average battery % of online robots. */
  avgBattery: number;
  /** Average max joint temperature °C of online robots. */
  avgTemp: number;
}

/** Trend rolling-window cap: 2s × 90 ≈ 3 minutes. */
export const FLEET_SAMPLE_LIMIT = 90;

/**
 * Compute one fleet sample point from the current store snapshot.
 *
 * The metric matches HealthSummaryHeader's top bar exactly (online only counts is_online; active alerts = severity≥2),
 * to avoid "one number on the top bar, another on the trend line".
 */
export function computeFleetSample(state: {
  robots: Map<string, RobotTelemetry>;
  alerts: RobotAlert[];
}): FleetSample {
  let online = 0;
  let batterySum = 0;
  let tempSum = 0;
  for (const r of state.robots.values()) {
    if (isRobotOnline(r)) {
      online += 1;
      batterySum += r.battery_pct;
      tempSum += r.max_joint_temp;
    }
  }
  let activeAlerts = 0;
  for (const a of state.alerts) if (a.severity >= 2) activeAlerts += 1;
  const total = state.robots.size;
  return {
    t: Date.now(),
    onlineRate: total === 0 ? 0 : Math.round((online / total) * 1000) / 10,
    activeAlerts,
    avgBattery: online === 0 ? 0 : Math.round((batterySum / online) * 10) / 10,
    avgTemp: online === 0 ? 0 : Math.round((tempSum / online) * 10) / 10,
  };
}

/** 100 standard robot IDs: humanoid-001 ~ humanoid-100 (spec/00 §1). */
export function buildRobotIds(count = 100): string[] {
  return Array.from({ length: count }, (_, i) => `humanoid-${String(i + 1).padStart(3, '0')}`);
}

/**
 * Offline placeholder telemetry when there is no data (requirements 6: the 100 robots must not be blank).
 * The joint dictionary is zero-filled using the 24 key names from spec/00 §5.5,
 * otherwise JointHeatmap opening an empty robot would render an empty grid.
 */
export function offlinePlaceholder(robotId: string): RobotTelemetry {
  const zeros: Record<string, number> = {};
  for (const name of JOINT_NAMES) zeros[name] = 0;
  return {
    robot_id: robotId,
    status_code: 0,
    battery_pct: 0,
    max_joint_temp: 0,
    joint_temperatures: { ...zeros },
    joint_torques: { ...zeros },
    joint_positions: { ...zeros },
    error_code: 0,
    ts: 0,
    // ISSUE #77: the placeholder card **must** explicitly declare is_online=false, rather than letting the
    // downstream guess from "status_code is 0". Placeholder and real-offline are two different things
    // (former = never received data, latter = received but stopped), but both have the online flag false ——
    // only this one field is the single site-wide authority on online status.
    is_online: false,
    last_seen: null,
  };
}

interface RobotState {
  /** robot_id -> latest telemetry (100 robots resident; offline placeholder when not reported). */
  robots: Map<string, RobotTelemetry>;
  /** Alert stream, newest alert on top (prepend). */
  alerts: RobotAlert[];
  /** WS connection status, for the top-bar status dot. */
  wsStatus: 'connecting' | 'open' | 'closed';
  /** Currently opened Drawer target. */
  selectedRobotId: string | null;
  /** Fleet time-series rolling buffer (UI trend line; one point per 2s, see computeFleetSample). */
  fleetSamples: FleetSample[];

  /** snapshot: full fill (spec/00 §5.2 ①). */
  applySnapshot: (list: RobotTelemetry[]) => void;
  /** telemetry: incremental (spec/00 §5.2 ②) —— called in batch after the 100ms throttle. */
  applyTelemetryBatch: (list: RobotTelemetry[]) => void;
  /** alert: prepend to the alert stream (spec/00 §5.2 ③). */
  pushAlert: (alert: RobotAlert) => void;
  /**
   * Called after ack/withdraw (Order 17): update an alert's ack state in place.
   *
   * Why by alert_id rather than index: the alert stream is prepended, so the index drifts over time;
   * alert_id is the stable primary key. If not found (WS and REST views out of sync) silently skip ——
   * the UI's authority is the next REST fetch; this only makes "the button you just clicked" go gray immediately.
   */
  applyAlertAck: (alertId: number, patch: Partial<RobotAlert>) => void;
  setWsStatus: (status: 'connecting' | 'open' | 'closed') => void;
  selectRobot: (robotId: string | null) => void;
  /** Append one fleet sample point (ring buffer; drop the oldest past FLEET_SAMPLE_LIMIT). */
  pushFleetSample: (sample: FleetSample) => void;
}

/** Offline-placeholder initial value, guaranteeing 100 offline cards on first paint (WS not yet connected; orders/05 acceptance). */
function initialRobots(): Map<string, RobotTelemetry> {
  const map = new Map<string, RobotTelemetry>();
  for (const id of buildRobotIds(100)) map.set(id, offlinePlaceholder(id));
  return map;
}

export const useRobotStore = create<RobotState>((set) => ({
  robots: initialRobots(),
  alerts: [],
  wsStatus: 'connecting',
  selectedRobotId: null,
  fleetSamples: [],

  applySnapshot: (list) =>
    set((state) => {
      // Full fill: keep the existing 100-ID skeleton, overwrite with the snapshot, missing ones fall back to offline placeholder.
      const next = new Map(state.robots);
      for (const id of next.keys()) next.set(id, offlinePlaceholder(id));
      for (const item of list) next.set(item.robot_id, item);
      return { robots: next };
    }),

  applyTelemetryBatch: (list) =>
    set((state) => {
      if (list.length === 0) return state;
      // Structural sharing (ISSUE #75): first do a **reference-level dedupe probe** —— if every entry in this frame
      // shares a reference with the existing object in state (duplicate push / idle frame), the whole Map need not be
      // rebuilt: return the original state so zustand gives zero notifications and all subscribers zero re-renders.
      // Only a frame with a changed reference gets one Map copy + O(n) overwrite (n ≤ 100) —— the copy itself is cheap;
      // the real cost is in subscription notification, and notification's prerequisite must be "something actually changed".
      let touched = false;
      for (const item of list) {
        if (state.robots.get(item.robot_id) !== item) {
          touched = true;
          break;
        }
      }
      if (!touched) return state;
      const next = new Map(state.robots);
      for (const item of list) next.set(item.robot_id, item);
      return { robots: next };
    }),

  pushAlert: (alert) =>
    set((state) => ({ alerts: [alert, ...state.alerts].slice(0, ALERT_BUFFER_LIMIT) })),

  applyAlertAck: (alertId, patch) =>
    set((state) => {
      // Only swap the array reference if content actually changed: otherwise every click would re-render the whole
      // alert table, violating R-01 §6's "the rest of the UI should stay quiet" performance guardrail.
      let hit = false;
      const next = state.alerts.map((a) => {
        if (a.alert_id !== alertId) return a;
        hit = true;
        return { ...a, ...patch };
      });
      return hit ? { alerts: next } : state;
    }),

  setWsStatus: (wsStatus) => set({ wsStatus }),

  selectRobot: (selectedRobotId) => set({ selectedRobotId }),

  pushFleetSample: (sample) =>
    set((state) => ({
      fleetSamples: [...state.fleetSamples, sample].slice(-FLEET_SAMPLE_LIMIT),
    })),
}));

/**
 * Selectors for components to derive on demand (avoids writing Map-traversal logic inside components).
 *
 * ISSUE #77: online or not reads **only the backend-issued `is_online`** (see `isRobotOnline`),
 * no longer inferred from `r.status_code === 0`. The old writing treated "the robot's self-reported last frame state"
 * as the online evidence, so every robot that stopped reporting was counted as online —— the dashboard's
 * "online 100 / offline 0" illusion came from exactly this.
 *
 * Three-way semantics (do not collapse into a single `offline` number):
 *   - online  —— the backend received telemetry within the window;
 *   - offline —— **received before, but already stopped** (lost contact, needs ops on-site);
 *   - never   —— never received any telemetry (the 100 pre-seeded by init.sql, mostly start in this state).
 * The latter two are merged in the "online rate", but must be distinguished on the cards: "offline" would be misread
 * as "it was just off", while "lost contact" is "it was working a moment ago and now we can't reach it" —— completely
 * different handling actions.
 */
export function selectStatusCounts(state: { robots: Map<string, RobotTelemetry> }): {
  total: number;
  online: number;
  offline: number;
  /** Received telemetry before, but exceeded the online threshold (backend is_online=false and last_seen non-null). */
  stale: number;
} {
  let online = 0;
  let stale = 0;
  let offline = 0;
  for (const r of state.robots.values()) {
    if (isRobotOnline(r)) {
      online += 1;
    } else {
      offline += 1;
      if (r.last_seen !== null && r.last_seen !== undefined) stale += 1;
    }
  }
  return { total: state.robots.size, online, offline, stale };
}

export type { RobotStatus };
