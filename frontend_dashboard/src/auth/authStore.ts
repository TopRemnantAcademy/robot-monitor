// 认证状态（订单 15 T4）
//
// 只存"要不要显示登录页"和"角色是什么"两件事。token 不在这里（见 session.ts 的说明）。

import { create } from 'zustand';
import { fetchCurrentUser, logout as apiLogout } from './api';
import { hasToken } from './api';
import type { Role } from './types';

/** `unknown` = 尚未探测（首屏在跑 /users/me），用于三态渲染避免登录页闪一下。 */
export type AuthStatus = 'unknown' | 'anonymous' | 'authenticated';

interface AuthState {
  status: AuthStatus;
  email: string | null;
  role: Role | null;

  /** 首屏启动流程：有 token 就换用户信息，没有就直接匿名。
   *  登录成功后也复用它（拿到 token 后走同一条 /users/me 路径取 role）。 */
  bootstrap: () => Promise<void>;
  /** 登出：先通知后端，再清本地状态。 */
  signOut: () => Promise<void>;
  /** 401 拦截回调：无网络请求，纯状态回退。 */
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
      // token 过期/被拒（fetchCurrentUser 内部已触发 clearToken）→ 回登录页。
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
 * 管理员判定（订单 17 的按钮显隐会用它）。
 *
 * ⚠️ 这**只是 UI 裁剪**，不是安全边界：绕过前端直接打接口依然会被后端
 * `require_role(Role.ADMIN)` 挡住（403）。前端的价值是"不让用户看见点不动的东西"。
 */
export function useIsAdmin(): boolean {
  return useAuthStore((s) => s.role === 'admin');
}
