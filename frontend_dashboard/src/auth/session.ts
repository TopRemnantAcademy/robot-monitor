// Auth state (Order 15 T4)
//
// [Why sessionStorage instead of localStorage]
// The dashboard is a **public terminal**: a machine hung on the wall in the workshop that anyone walking by can operate.
// localStorage persists across tabs and sessions — after the previous shift logs out, the next person opening the browser is still logged in;
// sessionStorage is cleared when the tab closes, matching the physical semantics of "one person per shift, leave when done".
// Order 15's forbidden items explicitly prohibit localStorage.
//
// [Why the token is not put into the Zustand store]
// The token is only read in two places: assembling the fetch request header and building the WS URL. Putting it in the global store would
// trigger subscription notifications across the entire component tree on every login/logout, while it itself does not participate in rendering.

const TOKEN_KEY = 'robotmonitor.jwt';

/** Read token within the session (returns null if not logged in). */
export function getToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // In private mode / when storage is disabled, sessionStorage throws. Returning null = not logged in,
    // so the user sees the login page instead of the whole app white-screening.
    return null;
  }
}

/** Write token. Only called after successful login. */
export function setToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Same as above: if it can't be written, just stay "not logged in"; don't let it bubble up as an uncaught exception.
  }
}

/** Clear token. Shared single exit point for logout and 401 interception. */
export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* see getToken */
  }
}

/** Request headers with the Authorization header. When there's no token, don't add it, letting the backend return 401. */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken();
  return {
    ...(extra ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}
