// Auth-related backend calls (Order 15 T4)
//
// Only wraps the three actions: login, logout, /users/me — business requests (/robots/history)
// stay at their own call sites, just add headers via `authHeaders()`, no need to wrap another layer.

import { authHeaders, clearToken, getToken, setToken } from './session';
import type { CurrentUser, LoginResult } from './types';
import type { SupportedRobotsResponse } from '../types/robot';

/** REST base URL: Vite only exposes VITE_ prefix (orders/05 forbidden item). */
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

/** Unified 401/403 exit. Register a callback to let the Store layer change UI state (avoid importing store here and creating a cycle). */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** Injected by App: what to do when 401 is received (clear token + return to login state). */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

/**
 * Trigger the "unauthorized" flow: clear token + notify subscribers to return to login state.
 * Shared internally by api.ts and business call sites (e.g. the Drawer's 401 branch),
 * **do not** replace it with a hand-written `setUnauthorizedHandler(() => {})` — that clears the global handler.
 */
export function notifyUnauthorized(): void {
  clearToken();
  onUnauthorized?.();
}

/**
 * Login. On success the token is stored in sessionStorage.
 *
 * ⚠️ Must use `application/x-www-form-urlencoded`: fastapi-users'
 * `/auth/jwt/login` uses OAuth2PasswordRequestForm (OpenAPI standard),
 * sending JSON yields 422. The field name is `username` (value is the email), not `email`.
 */
export async function login(email: string, password: string): Promise<LoginResult> {
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_BASE}/auth/jwt/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });

  if (res.status === 400) {
    // The backend returns 400 uniformly for "wrong password / user not found" (not distinguished, to avoid account enumeration).
    throw new Error('Email or password is incorrect');
  }
  if (!res.ok) {
    throw new Error(`Login failed (HTTP ${res.status})`);
  }

  const data = (await res.json()) as { access_token: string; token_type: string };
  setToken(data.access_token);
  return { accessToken: data.access_token, tokenType: data.token_type };
}

/** Fetch the current user (including role). The frontend uses this to hide admin buttons. */
export async function fetchCurrentUser(): Promise<CurrentUser> {
  const res = await fetch(`${API_BASE}/users/me`, { headers: authHeaders() });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('Session expired, please log in again');
  }
  if (!res.ok) throw new Error(`Failed to fetch user info (HTTP ${res.status})`);
  return (await res.json()) as CurrentUser;
}

/**
 * Logout. JWT is stateless, so discarding it server-side is meaningless — what actually takes effect is clearing the local token.
 * We still call the endpoint so that "logout" leaves a record in the server logs (auditing needs it).
 * If the endpoint fails it should not block the user from leaving, so errors are simply swallowed.
 */
export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/jwt/logout`, {
      method: 'POST',
      headers: authHeaders(),
    });
  } catch {
    /* still clear local token when network is down */
  }
  notifyUnauthorized();
}

/** Lets business call sites check "is logged in", avoiding touching sessionStorage directly each time. */
export function hasToken(): boolean {
  return getToken() !== null;
}

/**
 * Fetch the "platform-supported robot models" catalog + registered fleet composition (GET /supported-robots).
 * Readable from viewer role up; used for the "supported models" display in both the UI and admin UI.
 */
export async function fetchSupportedRobots(): Promise<SupportedRobotsResponse> {
  const res = await fetch(`${API_BASE}/supported-robots`, { headers: authHeaders() });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error('Session expired, please log in again');
  }
  if (!res.ok) throw new Error(`Failed to fetch supported models (HTTP ${res.status})`);
  return (await res.json()) as SupportedRobotsResponse;
}
