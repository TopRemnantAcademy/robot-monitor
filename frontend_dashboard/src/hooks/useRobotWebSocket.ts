// spec/03 §6 —— 防卡顿核心：100ms 节流 + 按 type 三分支 + 指数退避重连
//
// 【为什么必须节流（AGENTS.md §3 性能护栏）】
// 100 台机器人 × 10Hz = 1000 msg/s。若在 onmessage 里直接 setState：
//   每帧 1000 次 store 写入 → 1000 次订阅通知 → React 重渲染风暴，主线程打满，页面直接卡死。
// 本实现的解法：
//   1) onmessage 只往 useRef<Map> 里写（同步、O(1)、不触发任何 React 订阅）；
//   2) 独立的 setInterval(100ms) 每帧把 Map 里的「最新值」批量刷一次 Zustand。
//   于是无论进来多少条消息，React 每秒最多只收到 10 次通知，且每台机器人只保留最新一帧
//   （中间帧被自然去重合并，因为 Map 覆盖写）。这正是 spec/03 §6 要求的形态。

import { useEffect, useRef } from 'react';
import { useRobotStore } from '../store/robotStore';
import { getToken } from '../auth/session';
import { useAuthStore } from '../auth/authStore';
import type { RobotAlert, RobotTelemetry, WsEnvelope } from '../types/robot';
// 订单 18 T4：WS 信封的开发模式校验（生产不校验，见 wsEnvelope.ts 的性能说明）
import { checkEnvelope, initWsValidator } from '../lib/wsEnvelope';

/** 节流刷库间隔：100ms = 10Hz（spec/03 §6） */
export const FLUSH_INTERVAL_MS = 100;
/** 重连退避上限（spec/03 §6：1s→2s→…→10s） */
export const RECONNECT_MAX_MS = 10_000;
/** 退避起点 */
export const RECONNECT_MIN_MS = 1_000;
/**
 * 连接「稳定」判据：连上后存活满 5s 才把退避重置回 1s。
 * 若一 onopen 就重置，则服务端反复抖动（连上→被踢→连上）时退避永远停在 1s，
 * 退化成固定 1s 高频重连，既压垮服务端也违背「指数退避」的设计意图。
 */
export const RECONNECT_STABLE_MS = 5_000;
/**
 * WS close code 1008 = policy violation（RFC 6455 §7.4.1）。
 * 订单 15：后端在 token 缺失/非法时用它关闭连接，前端据此区分
 * "凭据被拒"（回登录页）与"网络断了"（退避重连）。
 */
export const WS_CLOSE_POLICY_VIOLATION = 1008;

/**
 * 解析 WS 地址：ws://{VITE_WS_HOST}:8000/ws/robots
 * Vite 只暴露 VITE_ 前缀（orders/05 禁止项：不得使用 WS_HOST）。
 *
 * @param token JWT。订单 15：后端要求 `?token=<JWT>`，缺失/非法会以 close 1008 拒绝。
 */
export function resolveWsUrl(token: string | null = getToken()): string {
  const host = import.meta.env.VITE_WS_HOST ?? 'localhost';
  const port = import.meta.env.VITE_WS_PORT ?? '8000';
  let base: string;
  // 兼容传入带协议/带端口的 VITE_WS_HOST（如 ws://1.2.3.4:8000）
  if (host.startsWith('ws://') || host.startsWith('wss://')) {
    const trimmed = host.replace(/\/+$/, '');
    base = /:\d+$/.test(trimmed) ? `${trimmed}/ws/robots` : `${trimmed}:${port}/ws/robots`;
  } else {
    base = `ws://${host}:${port}/ws/robots`;
  }
  // 浏览器 WebSocket API 无法自定义请求头，凭据只能进 query。
  // encodeURIComponent 必须加：JWT 虽是 base64url 但含 '.'，且未来换算法可能带 '+/='
  // 这些字符在 query 里会被误解，进而导致服务端收到的 token 与签发的不是同一个。
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

export function useRobotWebSocket(): void {
  // 最新遥测缓冲：robot_id -> 最新一帧。不参与渲染，故用 ref。
  const latestDataRef = useRef<Map<string, RobotTelemetry>>(new Map());
  // 告警是低频事件（非 10Hz），但仍统一走同一次 flush，避免额外渲染路径。
  const alertBufferRef = useRef<RobotAlert[]>([]);
  const socketRef = useRef<WebSocket | null>(null);
  const retryDelayRef = useRef<number>(RECONNECT_MIN_MS);
  const closedByUnmountRef = useRef<boolean>(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const stableTimerRef = useRef<number | null>(null);

  // ---- 唯一的刷库出口：100ms 一次 ----
  useEffect(() => {
    const timer = window.setInterval(() => {
      const store = useRobotStore.getState();

      // telemetry / snapshot 用同一入口批量合并
      if (latestDataRef.current.size > 0) {
        store.applyTelemetryBatch(Array.from(latestDataRef.current.values()));
        latestDataRef.current.clear();
      }

      // alert 逐条 prepend，保持时间顺序（缓冲区未满时数量很少）
      if (alertBufferRef.current.length > 0) {
        for (const alert of alertBufferRef.current) store.pushAlert(alert);
        alertBufferRef.current = [];
      }
    }, FLUSH_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, []);

  // ---- WS 连接 + 分发 + 重连 ----
  useEffect(() => {
    closedByUnmountRef.current = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    // 开发模式：先把 ajv 校验器预热好（动态 import，生产分支不会执行）。
    void initWsValidator();

    const handleEnvelope = (raw: string) => {
      let envelope: WsEnvelope;
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        // 非法 JSON 直接丢弃，不污染状态树
        return;
      }
      // 开发模式契约校验：**只警告不丢弃** —— 丢弃会让开发期页面白屏，
      // 而"契约漂移"这件事应该是看得见的告警，不该伪装成"没收到数据"。
      // checkEnvelope 在生产模式恒返回 {ok:true} 且零开销（见 wsEnvelope.ts）。
      const check = checkEnvelope(parsed);
      if (!check.ok) {
        console.error('[ws-contract] 信封不符合 docs/contracts/ws-envelope.schema.json:', check.errors);
      }
      envelope = parsed as WsEnvelope;
      if (!envelope || typeof envelope !== 'object' || !('type' in envelope)) return;

      switch (envelope.type) {
        // ① 首次连接：100 台全量快照 → 立即全量填充（一次 set，不违反节流原则）
        case 'snapshot': {
          const robots = envelope.data?.robots;
          if (Array.isArray(robots)) useRobotStore.getState().applySnapshot(robots);
          break;
        }
        // ② 增量遥测 → 只写 ref，等 100ms 帧统一刷库
        case 'telemetry': {
          const item = envelope.data;
          if (item && typeof item.robot_id === 'string') {
            latestDataRef.current.set(item.robot_id, item);
          }
          break;
        }
        // ③ 告警 → 入缓冲，等 100ms 帧 prepend 到告警流
        case 'alert': {
          const alert = envelope.data;
          if (alert && typeof alert.robot_id === 'string') {
            alertBufferRef.current.push(alert);
          }
          break;
        }
        default:
          break;
      }
    };

    const connect = () => {
      const url = resolveWsUrl();
      useRobotStore.getState().setWsStatus('connecting');

      const socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        // 只有「稳定连上一段时间」才重置退避，避免抖动场景下退避永不增长（见 RECONNECT_STABLE_MS）
        stableTimerRef.current = window.setTimeout(() => {
          retryDelayRef.current = RECONNECT_MIN_MS;
        }, RECONNECT_STABLE_MS);
        useRobotStore.getState().setWsStatus('open');
      };

      socket.onmessage = (event: MessageEvent<string>) => handleEnvelope(event.data);

      socket.onerror = () => {
        // 出错后 onclose 一定会触发，重连逻辑统一收敛在 onclose，避免双重重连
        socket.close();
      };

      socket.onclose = (event: CloseEvent) => {
        if (closedByUnmountRef.current) return;
        // 未活到稳定阈值就断了 → 这次不算成功，取消重置
        if (stableTimerRef.current !== null) {
          window.clearTimeout(stableTimerRef.current);
          stableTimerRef.current = null;
        }
        useRobotStore.getState().setWsStatus('closed'); // 连接点变红

        // 订单 15：1008 = policy violation = 后端判 token 无效/过期。
        // **不能退避重连**：token 不会因为多试几次就变有效，只会把日志刷满、
        // 并把一个"该重新登录"的信号伪装成"网络抖动"。正确动作是回登录页。
        if (event.code === WS_CLOSE_POLICY_VIOLATION) {
          useAuthStore.getState().markAnonymous();
          return;
        }

        const delay = retryDelayRef.current;
        // 指数退避 1s→2s→4s→8s→10s（封顶）
        retryDelayRef.current = Math.min(delay * 2, RECONNECT_MAX_MS);
        clearReconnectTimer();
        reconnectTimerRef.current = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      closedByUnmountRef.current = true;
      clearReconnectTimer();
      if (stableTimerRef.current !== null) {
        window.clearTimeout(stableTimerRef.current);
        stableTimerRef.current = null;
      }
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close();
      }
    };
  }, []);
}
