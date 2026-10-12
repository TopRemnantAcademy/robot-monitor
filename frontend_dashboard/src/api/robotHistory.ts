// 订单 26（I-20）：Drawer 历史查询的服务端调用点（原内联在 RobotDetailDrawer.tsx）。
//
// 只负责「取数 + 归一化错误」，缓存/重试交给 react-query（见 lib/queryClient.ts）。
// 401 仍走全局出口 notifyUnauthorized() —— 这是订单 15 定下的唯一处理姿势。

import { API_BASE } from '../auth/api';
import { notifyUnauthorized } from '../auth/api';
import { authHeaders } from '../auth/session';
import type { HistoryPoint, RobotHistoryResponse } from '../types/robot';

/** 历史查询窗口（spec/00 §5.3：默认最近 5 分钟，10s 分辨率） */
export const HISTORY_MINUTES = 5;
export const HISTORY_RESOLUTION = '10s';

/**
 * Drawer 历史窗口可选项（分钟）。
 * 后端 `GET /robots/{id}/history` 的 `minutes` 查询参数**无上限约束**
 * （backend main.py: `minutes: int = 5`，`span_s = max(1, minutes) * 60`），
 * 因此加窗口纯属前端选择，不需要任何后端改动。
 */
export const HISTORY_WINDOW_OPTIONS = [5, 30, 60] as const;

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

/**
 * react-query queryKey：以 (robot_id, minutes) 为维度缓存。
 * minutes 进 key 是刻意的——切换窗口时 key 变化 → react-query 自动重新拉取，
 * 不需要手写 effect；同窗口内同机器人复用缓存、换机器人/换窗口各自缓存。
 */
export function robotHistoryKey(
  robotId: string,
  minutes: number = HISTORY_MINUTES,
): readonly [string, string, number] {
  return ['robot-history', robotId, minutes];
}

/**
 * GET {API_BASE}/robots/{id}/history?minutes=N&resolution=10s
 *
 * 401 → notifyUnauthorized()（清 token 回登录页）+ 抛错。抛出的错误带 status=401，
 * queryClient 据此判定 4xx 不重试，避免"登录过期"时反复重试刷屏。
 */
export async function fetchRobotHistory(
  robotId: string,
  signal?: AbortSignal,
  minutes: number = HISTORY_MINUTES,
): Promise<HistoryPoint[]> {
  const url = `${API_BASE}/robots/${encodeURIComponent(robotId)}/history?minutes=${minutes}&resolution=${HISTORY_RESOLUTION}`;
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
