// Backend calls for the alert lifecycle (Order 17 T4).
//
// Division of labor with auth/api.ts: that file is auth actions, this is business read/write.
// Reuse API_BASE and authHeaders() (both from the auth layer), don't redefine the base URL.

import { authHeaders } from './session';
import { API_BASE, notifyUnauthorized } from './api';
import type { AlertFilter, AlertPage, AckResult } from '../types/robot';

/** Unified 401 handling: clear token → return to login state (reuse the existing auth-layer exit, no new one). */
function handle401(res: Response): void {
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('Session expired, please log in again');
  }
}

/** Filter → query string. `unacked`→ack=false, `escalated`→escalated=true. */
function filterQuery(filter: AlertFilter): string {
  if (filter === 'unacked') return 'ack=false';
  if (filter === 'escalated') return 'escalated=true';
  return '';
}

export interface FetchAlertsParams {
  filter?: AlertFilter;
  limit?: number;
  offset?: number;
  /**
   * Suppression-dimension filter (Order 35):
   *   true  → only pull suppressed; false → exclude suppressed; omitted → don't send the param (backend returns the full set).
   *
   * This is orthogonal to `filter` (the latter is "which category to view", the former is "whether to include suppressed"),
   * so it is not merged into the `AlertFilter` union type — otherwise "unacknowledged + suppressed" cannot be expressed.
   */
  suppressed?: boolean;
}

/**
 * Paginated fetch of alert history.
 *
 * Why the alert stream has both WS and this REST data source:
 * WS is the real-time push of "new alerts" (incremental only), REST is the authoritative paginated view of "existing alerts"
 * (including post-hoc status changes like ack/escalated). WS ≠ full history, pagination must go through REST.
 */
export async function fetchAlerts({
  filter = 'all',
  limit = 20,
  offset = 0,
  suppressed,
}: FetchAlertsParams = {}): Promise<AlertPage> {
  const q = filterQuery(filter);
  const params = new URLSearchParams();
  if (q) {
    const [k, v] = q.split('=');
    params.set(k, v);
  }
  // When `suppressed=undefined`, **don't write this key**: URLSearchParams.set would serialize undefined
  // into the literal "undefined", and the backend `Query(default=None, ...)` receiving it → 422.
  if (suppressed !== undefined) params.set('suppressed', String(suppressed));
  params.set('limit', String(limit));
  params.set('offset', String(offset));

  const res = await fetch(`${API_BASE}/alerts?${params.toString()}`, {
    headers: authHeaders(),
  });
  handle401(res);
  if (!res.ok) throw new Error(`Failed to fetch alerts (HTTP ${res.status})`);
  return (await res.json()) as AlertPage;
}

/** Shared ack/unack implementation. `ack=true` acknowledges, `false` withdraws. */
async function setAck(alertId: number, ack: boolean): Promise<AckResult> {
  const verb = ack ? 'ack' : 'unack';
  const res = await fetch(`${API_BASE}/alerts/${alertId}/${verb}`, {
    method: 'POST',
    // Must include Content-Type + empty body: the backend AckRequest declares extra=forbid,
    // sending any field (especially ack_by) would 422 — this is a deliberate anti-forgery measure.
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: '{}',
  });
  handle401(res);
  if (res.status === 403) {
    throw new Error('Admin permission required to acknowledge alerts');
  }
  if (res.status === 404) {
    throw new Error('This alert does not exist (may have expired/been cleaned up)');
  }
  if (!res.ok) throw new Error(`Acknowledge failed (HTTP ${res.status})`);
  return (await res.json()) as AckResult;
}

/** Acknowledge an alert (requires admin). */
export function ackAlert(alertId: number): Promise<AckResult> {
  return setAck(alertId, true);
}

/** Withdraw acknowledgement (requires admin). */
export function unackAlert(alertId: number): Promise<AckResult> {
  return setAck(alertId, false);
}
