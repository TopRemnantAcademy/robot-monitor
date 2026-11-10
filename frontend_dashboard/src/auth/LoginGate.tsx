// Login gate (Order 15 T4)
//
// Form note: the work order says "this order can first add a gate in front of App.tsx", so this is not a standalone route page,
// but renders in three states at the App top level based on authStore.status:
//   unknown        → minimal placeholder (first screen probing token, avoid login-page flash)
//   anonymous      → login form
//   authenticated  → normal dashboard
//
// Styling reuses design tokens (spec/03 §3), no new color values introduced.

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
        Checking login status…
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
      // Login only exchanges for a token; the role is still inside the token —— reuse bootstrap's same path:
      // it writes to the store after getting the /users/me result. Calling it directly here avoids reading the API twice.
      await useAuthStore.getState().bootstrap();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
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
        <h1 style={{ ...FONT.title, color: COLORS.textPrimary }}>Robot Factory Monitor</h1>

        <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
          Email
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
          Password
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
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
