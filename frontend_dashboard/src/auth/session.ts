// 认证状态（订单 15 T4）
//
// 【为什么用 sessionStorage 而不是 localStorage】
// 大屏是**公共终端**：车间里一台挂在墙上的机器，谁走过去都能操作。
// localStorage 跨标签、跨会话常驻，上一班的人登出后下一个人开浏览器还是登录态；
// sessionStorage 随标签页关闭即清，符合"一人一班、用完即走"的物理语义。
// 订单 15 禁止项明确：禁止 localStorage。
//
// 【为什么 token 不放进 Zustand store】
// token 只在两处被读：fetch 组装请求头、WS 拼 URL。放进全局 store 会让
// 每次登录/登出触发整棵组件树的订阅通知，而它本身不参与渲染。

const TOKEN_KEY = 'robotmonitor.jwt';

/** 会话内读取 token（未登录返回 null）。 */
export function getToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // 隐私模式/存储被禁用时 sessionStorage 会抛。返回 null = 未登录，
    // 让用户看到登录页，而不是整个应用白屏。
    return null;
  }
}

/** 写入 token。仅登录成功后调用。 */
export function setToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // 同上：写不进去就只能维持"未登录"，不能让它冒泡成未捕获异常。
  }
}

/** 清除 token。登出与 401 拦截共用这一个出口。 */
export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 见 getToken */
  }
}

/** 带 Authorization 头的请求头。无 token 时不加，让后端返回 401。 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken();
  return {
    ...(extra ?? {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}
