// Reusable sign-in form (extracted from LoginGate for the landing-page corner flyout).
//
// Self-contained: on success it exchanges the credentials for a token via `login()` and then
// reuses the same `bootstrap()` path as the rest of the app, which flips authStore to
// `authenticated` and makes LoginGate render the dashboard. The parent does not need to react
// to success — the whole landing page unmounts when the user becomes authenticated.

import { useState } from 'react';
import { login } from './api';
import { useAuthStore } from './authStore';
import { useI18n } from '../i18n/I18nProvider';
import { COLORS, FONT, RADIUS } from '../theme/tokens';

interface LoginFormProps {
  /** Card title override (defaults to the platform brand). */
  title?: string;
  /** Autofocus the email field on mount (used when shown in the corner flyout). */
  autoFocus?: boolean;
}

export default function LoginForm({ title, autoFocus }: LoginFormProps) {
  const { t } = useI18n();
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
      // Login only exchanges for a token; the role is still inside the token — reuse bootstrap's
      // same path: it writes to the store after getting the /users/me result.
      await useAuthStore.getState().bootstrap();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" style={{ width: '100%' }}>
      <h2 style={{ ...FONT.title, color: COLORS.textPrimary, fontSize: 16 }}>
        {title ?? t('landing.brand')}
      </h2>

      <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
        Email
        <input
          type="email"
          required
          autoFocus={autoFocus}
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="px-3 py-2"
          style={{
            backgroundColor: COLORS.bgBase,
            border: `1px solid ${COLORS.border}`,
            color: COLORS.textPrimary,
            borderRadius: RADIUS.card,
          }}
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
          style={{
            backgroundColor: COLORS.bgBase,
            border: `1px solid ${COLORS.border}`,
            color: COLORS.textPrimary,
            borderRadius: RADIUS.card,
          }}
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
        {busy ? 'Signing in…' : t('landing.signIn')}
      </button>
    </form>
  );
}
