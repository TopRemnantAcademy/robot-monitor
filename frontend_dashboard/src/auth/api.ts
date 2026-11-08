// 认证相关的后端调用（订单 15 T4）
//
// 只封装登录、登出、/users/me 三个动作 —— 业务请求（/robots/history）
// 留在各自的调用点，用 `authHeaders()` 加头即可，不必为它再包一层。

import { authHeaders, clearToken, getToken, setToken } from './session';
import type { CurrentUser, LoginResult } from './types';
import type { SupportedRobotsResponse } from '../types/robot';

/** REST 基址：Vite 只暴露 VITE_ 前缀（orders/05 禁止项）。 */
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

/** 401/403 的统一出口。注册回调让 Store 层去改 UI 状态（避免这里 import store 造成环）。 */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** 由 App 注入：收到 401 时该做什么（清 token + 回登录态）。 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

/**
 * 触发"未授权"流程：清 token + 通知订阅方回登录态。
 * 供 api.ts 内部与业务调用点（如 Drawer 的 401 分支）共用，
 * **不要**用手写 `setUnauthorizedHandler(() => {})` 代替 —— 那是把全局处理器清掉。
 */
export function notifyUnauthorized(): void {
  clearToken();
  onUnauthorized?.();
}

/**
 * 登录。成功后 token 落 sessionStorage。
 *
 * ⚠️ 必须用 `application/x-www-form-urlencoded`：fastapi-users 的
 * `/auth/jwt/login` 走 OAuth2PasswordRequestForm（OpenAPI 标准），
 * 发 JSON 会得到 422。字段名是 `username`（值填 email），不是 `email`。
 */
export async function login(email: string, password: string): Promise<LoginResult> {
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_BASE}/auth/jwt/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });

  if (res.status === 400) {
    // 后端对"口令错误/用户不存在"统一返回 400（不区分，避免账号枚举）。
    throw new Error('邮箱或密码不正确');
  }
  if (!res.ok) {
    throw new Error(`登录失败（HTTP ${res.status}）`);
  }

  const data = (await res.json()) as { access_token: string; token_type: string };
  setToken(data.access_token);
  return { accessToken: data.access_token, tokenType: data.token_type };
}

/** 取当前用户（含 role）。前端据此隐藏管理员按钮。 */
export async function fetchCurrentUser(): Promise<CurrentUser> {
  const res = await fetch(`${API_BASE}/users/me`, { headers: authHeaders() });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) throw new Error(`获取用户信息失败（HTTP ${res.status}）`);
  return (await res.json()) as CurrentUser;
}

/**
 * 登出。JWT 是无状态的，服务端丢弃没有意义 —— 真正生效的是本地清 token。
 * 仍调用该端点，是为了让"登出"在服务端日志里留下记录（审计需要）。
 * 端点失败也不该阻塞用户离开，故错误只吞掉。
 */
export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/jwt/logout`, {
      method: 'POST',
      headers: authHeaders(),
    });
  } catch {
    /* 网络不通时仍然要清本地 token */
  }
  notifyUnauthorized();
}

/** 供业务调用点判断"是否已登录"，避免每次直接摸 sessionStorage。 */
export function hasToken(): boolean {
  return getToken() !== null;
}

/**
 * 取「平台支持的机型」目录 + 在册机群构成（GET /supported-robots）。
 * viewer 起可读；用于 UI 与 admin UI 的「支持的机型」展示。
 */
export async function fetchSupportedRobots(): Promise<SupportedRobotsResponse> {
  const res = await fetch(`${API_BASE}/supported-robots`, { headers: authHeaders() });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) throw new Error(`获取支持的机型失败（HTTP ${res.status}）`);
  return (await res.json()) as SupportedRobotsResponse;
}
