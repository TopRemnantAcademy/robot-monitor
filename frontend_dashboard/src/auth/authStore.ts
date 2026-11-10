// Auth state (Order 15 T4)
//
// Only stores two things: "whether to show the login page" and "what the role is". The token is not here (see session.ts notes).

import { create } from 'zustand';
import { fetchCurrentUser, logout as apiLogout } from './api';
import { hasToken } from './api';
import type { Role } from './types';

/** `unknown` = not yet probed (first screen running /users/me), used for three-state rendering to avoid a login-page flash. */
export type AuthStatus = 'unknown' | 'anonymous' | 'authenticated';

interface AuthState {
  status: AuthStatus;
  email: string | null;
  role: Role | null;

  /** First-screen bootstrap flow: if there's a token, exchange it for user info; if not, go anonymous directly.
   *  Reused after successful login too (after getting the token, go down the same /users/me path to fetch role). */
  bootstrap: () => Promise<void>;
  /** Logout: notify the backend first, then clear local state. */
  signOut: () => Promise<void>;
  /** 401 interception callback: no network request, pure state fallback. */
  markAnonymous: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  status: 'unknown',
  email: null,
  role: null,

  bootstrap: async () => {
    if (!hasToken()) {
      set({ status: 'anonymous', email: null, role: null });
      return;
    }
    try {
      const user = await fetchCurrentUser();
      set({ status: 'authenticated', email: user.email, role: user.role });
    } catch {
      // token expired/rejected (fetchCurrentUser already triggered clearToken internally) → return to login page.
      set({ status: 'anonymous', email: null, role: null });
    }
  },

  signOut: async () => {
    await apiLogout();
    set({ status: 'anonymous', email: null, role: null });
  },

  markAnonymous: () => set({ status: 'anonymous', email: null, role: null }),
}));

/**
 * Admin check (Order 17's button show/hide uses it).
 *
 * ⚠️ This is **only UI trimming**, not a security boundary: bypassing the frontend and hitting the API directly
 * is still blocked by the backend's `require_role(Role.ADMIN)` (403). The frontend's value is "not letting users see things they can't click".
 */
export function useIsAdmin(): boolean {
  return useAuthStore((s) => s.role === 'admin');
}
