// ID-registration form. Loaded lazily by LandingPage via React.lazy so its JS chunk is only
// downloaded when the user actually opens "Create ID" (the work order: "download/load the
// registration component on demand").
//
// The backend /auth/register does NOT return a token, so after a successful registration we show
// a success state and let the user return to the sign-in flyout to log in.

import { useState } from 'react';
import { register } from './api';
import { useI18n } from '../i18n/I18nProvider';
import { COLORS, FONT, RADIUS } from '../theme/tokens';

interface RegisterFormProps {
  /** Called when the user dismisses the success state (returns to sign-in). */
  onBackToSignIn: () => void;
}

export default function RegisterForm({ onBackToSignIn }: RegisterFormProps) {
  const { t } = useI18n();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError(t('register.error.mismatch'));
      return;
    }
    if (password.length < 3) {
      setError(t('register.error.weak'));
      return;
    }
    setBusy(true);
    try {
      await register(email, password);
      setDone(true);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Registration failed';
      if (/already registered|REGISTER_USER_ALREADY_EXISTS/i.test(msg)) {
        setError(t('register.error.exists'));
      } else {
        setError(msg || t('register.error.generic'));
      }
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="flex flex-col gap-3" style={{ width: 320 }}>
        <p style={{ color: COLORS.statusNormal, ...FONT.metric }}>{t('register.success')}</p>
        <button
          type="button"
          onClick={onBackToSignIn}
          className="py-2 font-semibold"
          style={{ backgroundColor: COLORS.accent, color: '#FFFFFF', borderRadius: RADIUS.card }}
        >
          {t('register.backToSignIn')}
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3" style={{ width: 320 }}>
      <h2 style={{ ...FONT.title, color: COLORS.textPrimary, fontSize: 16 }}>{t('register.title')}</h2>

      <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
        {t('register.email')}
        <input
          type="email"
          required
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
        {t('register.password')}
        <input
          type="password"
          required
          autoComplete="new-password"
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

      <label className="flex flex-col gap-1" style={{ color: COLORS.textSecondary, ...FONT.metric }}>
        {t('register.confirm')}
        <input
          type="password"
          required
          autoComplete="new-password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
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
        {busy ? 'Creating…' : t('register.submit')}
      </button>
    </form>
  );
}
