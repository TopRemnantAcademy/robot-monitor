// Zustand 全局状态树（spec/03 §1 / §5 / §6）
//
// 关键性能约束（AGENTS.md §3 性能护栏）：
// 本 store 只暴露「批量入口」applyTelemetryBatch，由 useRobotWebSocket 的
// 100ms setInterval 每帧调用一次。100 台 × 10Hz = 1000 msg/s，若每条消息都 set()
// React 会收到 1000 次/秒的订阅通知，重渲染直接把主线程打满。
// 因此 WS 消息先写入 useRef<Map> 缓冲，10Hz 统一刷入此处。

import { create } from 'zustand';
import type { RobotAlert, RobotStatus, RobotTelemetry } from '../types/robot';
import { JOINT_NAMES, isRobotOnline } from '../types/robot';

/** 告警流滚动上限，防止长时间运行内存无限增长（200px 高度可见约 6 行） */
export const ALERT_BUFFER_LIMIT = 200;

/**
 * 机群时序采样点（UI 趋势线用，见 FleetHealthAnalytics）。
 *
 * 为什么不从 WS 帧直接算趋势：robots Map 是**当前快照**，没有历史；而 /history 只按
 * 单机器人查。机群级趋势（在线率/活跃告警随时间的走向）需要一段滚动时间序列，故用
 * 低频采样（2s 一点）在客户端累积 —— 与 100ms 刷库热路径完全解耦，采样时才 O(n) 遍历一次。
 */
export interface FleetSample {
  t: number;
  /** 在线率 0–100（保留 1 位小数） */
  onlineRate: number;
  /** severity ≥ 2 的活跃告警数 */
  activeAlerts: number;
  /** 在线机器人平均电量 % */
  avgBattery: number;
  /** 在线机器人平均最高关节温度 °C */
  avgTemp: number;
}

/** 趋势滚动窗口上限：2s × 90 ≈ 3 分钟。 */
export const FLEET_SAMPLE_LIMIT = 90;

/**
 * 从当前 store 快照算一个机群采样点。
 *
 * 口径与 HealthSummaryHeader 顶栏严格一致（在线只认 is_online、活跃告警 = severity≥2），
 * 避免「顶栏一个数、趋势线另一个数」。
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
    // ISSUE #77：占位卡**必须**显式声明 is_online=false，而不是靠"status_code 是 0"
    // 让下游去猜。占位与真实离线是两件事（前者=从未收到数据，后者=收到过但停了），
    // 但两者的在线标志都是 false —— 只有这一个字段是全站唯一的在线权威。
    is_online: false,
    last_seen: null,
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
  /** 机群时序采样滚动缓冲（UI 趋势线；2s 一个点，见 computeFleetSample） */
  fleetSamples: FleetSample[];

  /** snapshot：全量填充（spec/00 §5.2 ①） */
  applySnapshot: (list: RobotTelemetry[]) => void;
  /** telemetry：增量（spec/00 §5.2 ②）—— 由 100ms 节流后批量调用 */
  applyTelemetryBatch: (list: RobotTelemetry[]) => void;
  /** alert：prepend 到告警流（spec/00 §5.2 ③） */
  pushAlert: (alert: RobotAlert) => void;
  /**
   * 确认/撤回后被调（订单 17）：把某条告警的 ack 状态就地更新。
   *
   * 为什么按 alert_id 而非索引：告警流是 prepend 的，索引随时间漂移；
   * alert_id 是稳定主键。找不到（WS 与 REST 视图不同步时）就静默跳过 ——
   * UI 的权威来源是下一次 REST 拉取，这里只是让"刚点的那个按钮"立刻变灰。
   */
  applyAlertAck: (alertId: number, patch: Partial<RobotAlert>) => void;
  setWsStatus: (status: 'connecting' | 'open' | 'closed') => void;
  selectRobot: (robotId: string | null) => void;
  /** 追加一个机群采样点（环形缓冲，超 FLEET_SAMPLE_LIMIT 丢弃最旧）。 */
  pushFleetSample: (sample: FleetSample) => void;
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
  fleetSamples: [],

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
      // 结构性共享（ISSUE #75）：先做**引用级去重探测** —— 本帧里所有条目
      // 若都与 state 中现有对象同引用（重复推送 / 空转帧），整张 Map 无需重建，
      // 返回原 state 让 zustand 零通知、所有订阅者零重渲染。
      // 引用有变的帧才做一次 Map 拷贝 + O(n) 覆盖（n ≤ 100）—— 拷贝本身廉价，
      // 真正的花费在订阅通知，而通知的前提必须是"真有东西变了"。
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
      // 只有内容真的变了才换数组引用：否则每次点击都会让整张告警表重渲染，
      // 违背 R-01 §6「其余 UI 应静」的性能护栏。
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
 * 供组件按需派生的选择器（避免在组件内写 Map 遍历逻辑）。
 *
 * ISSUE #77：在线与否**只读后端下发的 `is_online`**（见 `isRobotOnline`），
 * 不再用 `r.status_code === 0` 反推。旧写法把「机器人自述的最后一帧状态」当成在线证据，
 * 于是停报的机器人每次都被算作在线 —— 大屏「在线 100 / 离线 0」正是这么来的。
 *
 * 语义三分（不要合成一个 `offline` 数字）：
 *   - online  —— 后端在窗口内收到过遥测；
 *   - offline —— **收到过、但已经停了**（失联，需要运维去现场）；
 *   - never   —— 从未收到过任何遥测（init.sql 预置的 100 台，大部分开局即此态）。
 * 后两者在「在线率」里合并，但在卡片上必须区分：「离线」会被误读为"本来就是关着的"，
 * 而"失联"是"刚才还在干活、现在联系不上了"，处置动作完全不同。
 */
export function selectStatusCounts(state: { robots: Map<string, RobotTelemetry> }): {
  total: number;
  online: number;
  offline: number;
  /** 收到过遥测、但已超出在线阈值（后端 is_online=false 且 last_seen 非空） */
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
