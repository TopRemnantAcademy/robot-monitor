// 登录门（订单 15 T4）
//
// 形态说明：派工单说"本单可先在 App.tsx 前置一个 gate"，故这里不做独立路由页，
// 而是在 App 顶层按 authStore.status 三态渲染：
//   unknown        → 极简占位（首屏在探 token，避免登录页闪一下）
//   anonymous      → 登录表单
//   authenticated  → 正常大屏
//
// 样式沿用 design token（spec/03 §3），不引入新的色值。

import { useState } from 'react';
import { login } from './api';
import { useAuthStore } from './authStore';
import { COLORS, FONT, RADIUS } from '../theme/tokens';

export default function LoginGate({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status);

  if (status === 'unknown') {
    return (
      <div
        className="h-screen w-screen flex items-center justify-center"
        style={{ backgroundColor: COLORS.bgBase, color: COLORS.textSecondary }}
      >
        正在检查登录状态…
      </div>
    );
  }

  if (status === 'anonymous') return <LoginForm />;

  return <>{children}</>;
}

function LoginForm() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      // 登录只换来 token，role 还在 token 里 —— 复用 bootstrap 的同一路径：
      // 它拿到 /users/me 的结果后写入 store。这里直接调用可避免读两次接口。
      await useAuthStore.getState().bootstrap();
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="h-screen w-screen flex items-center justify-center"
      style={{ backgroundColor: COLORS.bgBase }}
    >
      <form
        onSubmit={submit}
        className="flex flex-col gap-4 p-8"
        style={{
          backgroundColor: COLORS.surface,
          border: `1px solid ${COLORS.border}`,
          borderRadius: RADIUS.card,
          width: 340,
        }}
      >
        <h1 style={{ ...FONT.title, color: COLORS.textPrimary }}>机器人工厂监控</h1>

        <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
          邮箱
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="px-3 py-2"
            style={{ backgroundColor: COLORS.bgBase, border: `1px solid ${COLORS.border}`, color: COLORS.textPrimary, borderRadius: RADIUS.card }}
          />
        </label>

        <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
          密码
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="px-3 py-2"
            style={{ backgroundColor: COLORS.bgBase, border: `1px solid ${COLORS.border}`, color: COLORS.textPrimary, borderRadius: RADIUS.card }}
          />
        </label>

        {error && (
          <p role="alert" style={{ color: COLORS.statusFault, ...FONT.metric }}>
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="py-2 font-semibold disabled:opacity-60"
          style={{ backgroundColor: COLORS.accent, color: '#FFFFFF', borderRadius: RADIUS.card }}
        >
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
