// 告警闭环的后端调用（订单 17 T4）。
//
// 与 auth/api.ts 的分工：那里是认证动作，这里是业务读写。
// 复用 API_BASE 与 authHeaders()（都来自 auth 层），不重复定义基址。

import { authHeaders } from './session';
import { API_BASE, notifyUnauthorized } from './api';
import type { AlertFilter, AlertPage, AckResult } from '../types/robot';

/** 统一处理 401：清 token → 回登录态（复用 auth 层的既有出口，不另造一套）。 */
function handle401(res: Response): void {
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('登录已过期，请重新登录');
  }
}

/** 筛选 → 查询串。`unacked`→ack=false，`escalated`→escalated=true。 */
function filterQuery(filter: AlertFilter): string {
  if (filter === 'unacked') return 'ack=false';
  if (filter === 'escalated') return 'escalated=true';
  return '';
}

export interface FetchAlertsParams {
  filter?: AlertFilter;
  limit?: number;
  offset?: number;
}

/**
 * 分页拉取告警历史。
 *
 * 为什么告警流同时有 WS 与这个 REST 两路数据源：
 * WS 是「新告警」的实时推送（只给增量），REST 是「已有告警」的权威分页视图
 * （含 ack/escalated 等**事后变化**的状态）。WS ≠ 完整历史，Pagination 必须走 REST。
 */
export async function fetchAlerts({
  filter = 'all',
  limit = 20,
  offset = 0,
}: FetchAlertsParams = {}): Promise<AlertPage> {
  const q = filterQuery(filter);
  const params = new URLSearchParams();
  if (q) {
    const [k, v] = q.split('=');
    params.set(k, v);
  }
  params.set('limit', String(limit));
  params.set('offset', String(offset));

  const res = await fetch(`${API_BASE}/alerts?${params.toString()}`, {
    headers: authHeaders(),
  });
  handle401(res);
  if (!res.ok) throw new Error(`获取告警失败（HTTP ${res.status}）`);
  return (await res.json()) as AlertPage;
}

/** ack / unack 的公共实现。`ack=true` 确认，`false` 撤回。 */
async function setAck(alertId: number, ack: boolean): Promise<AckResult> {
  const verb = ack ? 'ack' : 'unack';
  const res = await fetch(`${API_BASE}/alerts/${alertId}/${verb}`, {
    method: 'POST',
    // 必须带 Content-Type + 空 body：后端 AckRequest 声明了 extra=forbid，
    // 传任何字段（尤其 ack_by）都会 422 —— 这是刻意的防伪造。
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: '{}',
  });
  handle401(res);
  if (res.status === 403) {
    throw new Error('需要管理员权限才能确认告警');
  }
  if (res.status === 404) {
    throw new Error('该告警不存在（可能已过期清理）');
  }
  if (!res.ok) throw new Error(`确认失败（HTTP ${res.status}）`);
  return (await res.json()) as AckResult;
}

/** 确认告警（需 admin）。 */
export function ackAlert(alertId: number): Promise<AckResult> {
  return setAck(alertId, true);
}

/** 撤回确认（需 admin）。 */
export function unackAlert(alertId: number): Promise<AckResult> {
  return setAck(alertId, false);
}
