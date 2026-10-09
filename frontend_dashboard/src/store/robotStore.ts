// Zustand 全局状态树（spec/03 §1 / §5 / §6）
//
// 关键性能约束（AGENTS.md §3 性能护栏）：
// 本 store 只暴露「批量入口」applyTelemetryBatch，由 useRobotWebSocket 的
// 100ms setInterval 每帧调用一次。100 台 × 10Hz = 1000 msg/s，若每条消息都 set()
// React 会收到 1000 次/秒的订阅通知，重渲染直接把主线程打满。
// 因此 WS 消息先写入 useRef<Map> 缓冲，10Hz 统一刷入此处。

import { create } from 'zustand';
import type { RobotAlert, RobotStatus, RobotTelemetry } from '../types/robot';
import { JOINT_NAMES } from '../types/robot';

/** 告警流滚动上限，防止长时间运行内存无限增长（200px 高度可见约 6 行） */
export const ALERT_BUFFER_LIMIT = 200;

/** 100 台标准 ID：humanoid-001 ~ humanoid-100（spec/00 §1） */
export function buildRobotIds(count = 100): string[] {
  return Array.from({ length: count }, (_, i) => `humanoid-${String(i + 1).padStart(3, '0')}`);
}

/**
 * 无数据时的离线占位遥测（requirements 6：100 台不得空白）。
 * 关节字典按 spec/00 §5.5 的 24 个键名生成 0 值，
 * 否则 JointHeatmap 打开空机器人会渲染出空网格。
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
  };
}

interface RobotState {
  /** robot_id -> 最新遥测（100 台常驻，未上报时为离线占位） */
  robots: Map<string, RobotTelemetry>;
  /** 告警流，新告警置顶（prepend） */
  alerts: RobotAlert[];
  /** WS 连接状态，用于顶栏状态点 */
  wsStatus: 'connecting' | 'open' | 'closed';
  /** 当前打开的 Drawer 目标 */
  selectedRobotId: string | null;

  /** snapshot：全量填充（spec/00 §5.2 ①） */
  applySnapshot: (list: RobotTelemetry[]) => void;
  /** telemetry：增量（spec/00 §5.2 ②）—— 由 100ms 节流后批量调用 */
  applyTelemetryBatch: (list: RobotTelemetry[]) => void;
  /** alert：prepend 到告警流（spec/00 §5.2 ③） */
  pushAlert: (alert: RobotAlert) => void;
  setWsStatus: (status: 'connecting' | 'open' | 'closed') => void;
  selectRobot: (robotId: string | null) => void;
}

/** 离线占位初值，保证首屏（WS 未连上时）就有 100 张离线卡（orders/05 验收） */
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

  applySnapshot: (list) =>
    set((state) => {
      // 全量填充：保留已有 100 个 ID 的骨架，用快照覆盖，缺失的回落为离线占位
      const next = new Map(state.robots);
      for (const id of next.keys()) next.set(id, offlinePlaceholder(id));
      for (const item of list) next.set(item.robot_id, item);
      return { robots: next };
    }),

  applyTelemetryBatch: (list) =>
    set((state) => {
      if (list.length === 0) return state;
      // 每帧只做一次 Map 拷贝 + O(n) 覆盖，n = 本帧变化的机器人数（≤100），
      // 而不是每条消息一次 set()，这是防 1000 msg/s 卡顿的关键。
      const next = new Map(state.robots);
      for (const item of list) next.set(item.robot_id, item);
      return { robots: next };
    }),

  pushAlert: (alert) =>
    set((state) => ({ alerts: [alert, ...state.alerts].slice(0, ALERT_BUFFER_LIMIT) })),

  setWsStatus: (wsStatus) => set({ wsStatus }),

  selectRobot: (selectedRobotId) => set({ selectedRobotId }),
}));

/** 供组件按需派生的选择器（避免在组件内写 Map 遍历逻辑） */
export function selectStatusCounts(state: { robots: Map<string, RobotTelemetry> }): {
  total: number;
  online: number;
  offline: number;
} {
  let online = 0;
  let offline = 0;
  for (const r of state.robots.values()) {
    if (r.status_code === 0) offline += 1;
    else online += 1;
  }
  return { total: state.robots.size, online, offline };
}

export type { RobotStatus };
