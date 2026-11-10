// spec/03 §6 —— anti-jank core: 100ms throttle + three branches by type + exponential backoff reconnect
//
// NOTE — Why throttling is mandatory (AGENTS.md §3 performance guardrail):
// 100 robots × 10Hz = 1000 msg/s. If you setState directly in onmessage:
//   every frame 1000 store writes → 1000 subscription notifications → React re-render storm, main thread maxed out, page freezes dead.
// This implementation's solution:
//   1) onmessage only writes into useRef<Map> (synchronous, O(1), triggers no React subscription);
//   2) a separate setInterval(100ms) batches "latest value" from the Map into Zustand once per frame.
//   So no matter how many messages come in, React receives at most 10 notifications per second, and each robot keeps only its latest frame
//   (intermediate frames are naturally deduplicated/merged because Map overwrites). This is exactly the shape spec/03 §6 requires.

import { useEffect, useRef } from 'react';
import { useRobotStore } from '../store/robotStore';
import { getToken } from '../auth/session';
import { useAuthStore } from '../auth/authStore';
import { countStoreCommit, countWsMessage } from '../lib/observability';
import type { RobotAlert, RobotTelemetry, WsEnvelope } from '../types/robot';
// Order 18 T4: WS envelope dev-mode validation (no validation in production, see wsEnvelope.ts performance note)
import { checkEnvelope, initWsValidator } from '../lib/wsEnvelope';

/** Throttle flush interval: 100ms = 10Hz (spec/03 §6) */
export const FLUSH_INTERVAL_MS = 100;
/** Reconnect backoff ceiling (spec/03 §6: 1s→2s→…→10s) */
export const RECONNECT_MAX_MS = 10_000;
/** Backoff starting point */
export const RECONNECT_MIN_MS = 1_000;
/**
 * Connection "stable" criterion: only reset backoff to 1s after surviving a full 5s post-connect.
 * If reset immediately on onopen, then when the server jitters repeatedly (connect→kicked→connect), backoff would forever stay at 1s,
 * degrading into a fixed 1s high-frequency reconnect, which both overloads the server and violates the "exponential backoff" design intent.
 */
export const RECONNECT_STABLE_MS = 5_000;
/**
 * WS close code 1008 = policy violation (RFC 6455 §7.4.1).
 * Order 15: the backend uses this to close the connection when token is missing/invalid; the frontend uses it to distinguish
 * "credential rejected" (go to login) from "network dropped" (backoff reconnect).
 */
export const WS_CLOSE_POLICY_VIOLATION = 1008;

/**
 * Resolve WS address: ws://{VITE_WS_HOST}:8000/ws/robots
 * Vite only exposes VITE_ prefix (orders/05 forbidden item: must not use WS_HOST).
 *
 * @param token JWT. Order 15: the backend requires `?token=<JWT>`; missing/invalid is rejected with close 1008.
 */
export function resolveWsUrl(token: string | null = getToken()): string {
  const host = import.meta.env.VITE_WS_HOST ?? 'localhost';
  const port = import.meta.env.VITE_WS_PORT ?? '8000';
  let base: string;
  // Compatible with VITE_WS_HOST passed in with protocol/port (e.g. ws://1.2.3.4:8000)
  if (host.startsWith('ws://') || host.startsWith('wss://')) {
    const trimmed = host.replace(/\/+$/, '');
    base = /:\d+$/.test(trimmed) ? `${trimmed}/ws/robots` : `${trimmed}:${port}/ws/robots`;
  } else {
    base = `ws://${host}:${port}/ws/robots`;
  }
  // Browser WebSocket API can't set custom request headers, credential can only go in the query.
  // encodeURIComponent is mandatory: although JWT is base64url it contains '.', and a future algorithm change may bring '+/='
  // these characters get misinterpreted in the query, leading to the server receiving a token different from the issued one.
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

export function useRobotWebSocket(): void {
  // Latest telemetry buffer: robot_id -> latest frame. Doesn't participate in rendering, so use ref.
  const latestDataRef = useRef<Map<string, RobotTelemetry>>(new Map());
  // Alerts are low-frequency (not 10Hz), but still unified into the same flush, to avoid an extra render path.
  const alertBufferRef = useRef<RobotAlert[]>([]);
  const socketRef = useRef<WebSocket | null>(null);
  const retryDelayRef = useRef<number>(RECONNECT_MIN_MS);
  const closedByUnmountRef = useRef<boolean>(false);
  const reconnectTimerRef = useRef<number | null>(null);
  const stableTimerRef = useRef<number | null>(null);

  // ---- The only store-flush exit: once per 100ms ----
  useEffect(() => {
    const timer = window.setInterval(() => {
      const store = useRobotStore.getState();

      // telemetry / snapshot merge through the same entry point in batch
      if (latestDataRef.current.size > 0) {
        store.applyTelemetryBatch(Array.from(latestDataRef.current.values()));
        // Order 16 T2: only count one storeCommit when a **non-empty batch was actually committed**.
        // If the count were placed outside the if, empty frames would also count → inflated denominator → ratio underestimated,
        // the evidence for "throttle works" would be weaker. Count semantics must align strictly with "did this commit happen".
        countStoreCommit();
        latestDataRef.current.clear();
      }

      // alerts prepended one by one, preserving time order (buffer count is small before full)
      if (alertBufferRef.current.length > 0) {
        for (const alert of alertBufferRef.current) store.pushAlert(alert);
        alertBufferRef.current = [];
      }
    }, FLUSH_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, []);

  // ---- WS connect + dispatch + reconnect ----
  useEffect(() => {
    closedByUnmountRef.current = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    // Dev mode: pre-warm the ajv validator first (dynamic import, the production branch never executes).
    void initWsValidator();

    const handleEnvelope = (raw: string) => {
      let envelope: WsEnvelope;
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        // Invalid JSON discarded directly, doesn't pollute the state tree
        return;
      }
      // Dev-mode contract validation: **warn only, don't discard** —— discarding would white-screen the page in dev,
      // and "contract drift" should be a visible warning, not disguised as "no data received".
      // checkEnvelope always returns {ok:true} with zero overhead in production (see wsEnvelope.ts).
      const check = checkEnvelope(parsed);
      if (!check.ok) {
        console.error('[ws-contract] envelope does not match docs/contracts/ws-envelope.schema.json:', check.errors);
      }
      envelope = parsed as WsEnvelope;
      if (!envelope || typeof envelope !== 'object' || !('type' in envelope)) return;

      switch (envelope.type) {
        // ① First connection: 100-robot full snapshot → immediately full-fill (one set, doesn't violate the throttle principle)
        case 'snapshot': {
          const robots = envelope.data?.robots;
          if (Array.isArray(robots)) useRobotStore.getState().applySnapshot(robots);
          break;
        }
        // ② Incremental telemetry → only write to ref, flush into store at the 100ms frame
        case 'telemetry': {
          const item = envelope.data;
          if (item && typeof item.robot_id === 'string') {
            // Order 16 T2: count "how many telemetry WS received". This is the numerator of ratio ——
            // it counts **inbound messages**, decoupled from "how many times React was notified" (storeCommits).
            countWsMessage();
            latestDataRef.current.set(item.robot_id, item);
          }
          break;
        }
        // ③ Alert → into buffer, prepend to the alert stream at the 100ms frame
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
        // Only reset backoff after "stably connected for a while", to avoid backoff never growing in jitter scenarios (see RECONNECT_STABLE_MS)
        stableTimerRef.current = window.setTimeout(() => {
          retryDelayRef.current = RECONNECT_MIN_MS;
        }, RECONNECT_STABLE_MS);
        useRobotStore.getState().setWsStatus('open');
      };

      socket.onmessage = (event: MessageEvent<string>) => handleEnvelope(event.data);

      socket.onerror = () => {
        // After an error onclose will always fire; reconnect logic converges in onclose to avoid double reconnect
        socket.close();
      };

      socket.onclose = (event: CloseEvent) => {
        if (closedByUnmountRef.current) return;
        // Disconnected before reaching the stable threshold → this time doesn't count as success, cancel the reset
        if (stableTimerRef.current !== null) {
          window.clearTimeout(stableTimerRef.current);
          stableTimerRef.current = null;
        }
        useRobotStore.getState().setWsStatus('closed'); // connection dot turns red

        // Order 15: 1008 = policy violation = backend judges token invalid/expired.
        // **Must not backoff-reconnect**: a token won't become valid by trying a few more times, it only floods the logs,
        // and disguises a "should re-login" signal as "network jitter". The correct action is to go to the login page.
        if (event.code === WS_CLOSE_POLICY_VIOLATION) {
          useAuthStore.getState().markAnonymous();
          return;
        }

        const delay = retryDelayRef.current;
        // Exponential backoff 1s→2s→4s→8s→10s (capped)
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
