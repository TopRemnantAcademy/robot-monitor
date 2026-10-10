// 订单 26（I-20）：Drawer 历史查询的服务端调用点（原内联在 RobotDetailDrawer.tsx）。
//
// 只负责「取数 + 归一化错误」，缓存/重试交给 react-query（见 lib/queryClient.ts）。
// 401 仍走全局出口 notifyUnauthorized() —— 这是订单 15 定下的唯一处理姿势。

import { API_BASE } from '../auth/api';
import { notifyUnauthorized } from '../auth/api';
import { authHeaders } from '../auth/session';
import type { HistoryPoint, RobotHistoryResponse } from '../types/robot';

/** 历史查询窗口（spec/00 §5.3：最近 5 分钟，10s 分辨率） */
export const HISTORY_MINUTES = 5;
export const HISTORY_RESOLUTION = '10s';

/**
 * 带 HTTP 状态码的错误。
 * react-query 的重试策略依据 `status` 判断「4xx 不重试」（见 lib/queryClient.ts）。
 */
export class HistoryHttpError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`HTTP ${status}`);
    this.name = 'HistoryHttpError';
    this.status = status;
  }
}

/** react-query queryKey：以 robot_id 为维度缓存，同机器人复用、换机器人各自缓存。 */
export function robotHistoryKey(robotId: string): readonly [string, string] {
  return ['robot-history', robotId];
}

/**
 * GET {API_BASE}/robots/{id}/history?minutes=5&resolution=10s
 *
 * 401 → notifyUnauthorized()（清 token 回登录页）+ 抛错。抛出的错误带 status=401，
 * queryClient 据此判定 4xx 不重试，避免"登录过期"时反复重试刷屏。
 */
export async function fetchRobotHistory(
  robotId: string,
  signal?: AbortSignal,
): Promise<HistoryPoint[]> {
  const url = `${API_BASE}/robots/${encodeURIComponent(robotId)}/history?minutes=${HISTORY_MINUTES}&resolution=${HISTORY_RESOLUTION}`;
  const res = await fetch(url, { signal, headers: authHeaders() });

  if (res.status === 401) {
    // token 过期/被吊销：走全局出口，而不是当作一次普通加载失败。
    notifyUnauthorized();
    throw new HistoryHttpError(401);
  }
  if (!res.ok) throw new HistoryHttpError(res.status);

  const payload = (await res.json()) as RobotHistoryResponse;
  return Array.isArray(payload.points) ? payload.points : [];
}
