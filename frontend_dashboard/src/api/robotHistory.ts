// Order 26 (I-20): server-side call point for Drawer history queries (previously inlined in RobotDetailDrawer.tsx).
//
// Only responsible for "fetching data + normalizing errors"; caching/retry is delegated to react-query (see lib/queryClient.ts).
// 401 still goes through the global exit notifyUnauthorized() — this is the single handling approach defined by Order 15.

import { API_BASE } from '../auth/api';
import { notifyUnauthorized } from '../auth/api';
import { authHeaders } from '../auth/session';
import type { HistoryPoint, RobotHistoryResponse } from '../types/robot';

/** History query window (spec/00 §5.3: default last 5 minutes, 10s resolution) */
export const HISTORY_MINUTES = 5;
export const HISTORY_RESOLUTION = '10s';

/**
 * Available Drawer history window options (in minutes).
 * The backend `GET /robots/{id}/history` `minutes` query param has **no upper bound constraint**
 * (backend main.py: `minutes: int = 5`, `span_s = max(1, minutes) * 60`),
 * so adding a window is purely a frontend choice and requires no backend changes.
 */
export const HISTORY_WINDOW_OPTIONS = [5, 30, 60] as const;

/**
 * Error carrying an HTTP status code.
 * react-query's retry strategy uses `status` to decide "no retry on 4xx" (see lib/queryClient.ts).
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
 * react-query queryKey: cached by (robot_id, minutes) dimensions.
 * Including minutes in the key is deliberate — switching windows changes the key → react-query automatically refetches,
 * no manual effect needed; same window + same robot reuses cache, different robot/window each has its own cache.
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
 * 401 → notifyUnauthorized() (clear token, return to login) + throw. The thrown error carries status=401,
 * queryClient uses this to decide no retry on 4xx, avoiding repeated retries spamming on "login expired".
 */
export async function fetchRobotHistory(
  robotId: string,
  signal?: AbortSignal,
  minutes: number = HISTORY_MINUTES,
): Promise<HistoryPoint[]> {
  const url = `${API_BASE}/robots/${encodeURIComponent(robotId)}/history?minutes=${minutes}&resolution=${HISTORY_RESOLUTION}`;
  const res = await fetch(url, { signal, headers: authHeaders() });

  if (res.status === 401) {
    // token expired/revoked: go through global exit, not treated as a normal load failure.
    notifyUnauthorized();
    throw new HistoryHttpError(401);
  }
  if (!res.ok) throw new HistoryHttpError(res.status);

  const payload = (await res.json()) as RobotHistoryResponse;
  return Array.isArray(payload.points) ? payload.points : [];
}
