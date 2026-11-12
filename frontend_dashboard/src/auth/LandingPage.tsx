// Landing page shown to anonymous (not-yet-authenticated) visitors.
//
// Work order for the homepage redesign:
//   - Do NOT force an Email/Password form on first paint — show a normal landing page.
//   - The ID-registration component is downloaded/loaded on demand (React.lazy + Suspense), so its
//     JS chunk is only fetched when the user clicks "Create ID".
//   - ID sign-in lives in the TOP-RIGHT corner (a button that opens a compact flyout); it is not the
//     first thing the user sees.
//
// Visual layer (user request: add robot looks + make it flashy):
//   - FxBackdrop aurora + cyber grid behind everything.
//   - A floating, glowing RobotAvatar in the hero (theme-appropriate form factor).
//   - Orbitron display font + neon brand + staggered entrance + hover lift on cards.

import { Suspense, lazy, useState } from 'react';
import LoginForm from './LoginForm';
import LanguageSwitcher from '../i18n/LanguageSwitcher';
import RobotAvatar from '../components/RobotAvatar';
import FxBackdrop from '../components/FxBackdrop';
import { useI18n } from '../i18n/I18nProvider';
import { COLORS, FONT, RADIUS } from '../theme/tokens';

// Lazy: the registration form is a separate bundle, fetched only when opened.
const RegisterForm = lazy(() => import('./RegisterForm'));

export default function LandingPage() {
  const { t } = useI18n();
  const [signInOpen, setSignInOpen] = useState(false);
  const [registerOpen, setRegisterOpen] = useState(false);

  const features = [
    { title: t('landing.feature1Title'), desc: t('landing.feature1Desc') },
    { title: t('landing.feature2Title'), desc: t('landing.feature2Desc') },
    { title: t('landing.feature3Title'), desc: t('landing.feature3Desc') },
  ];

  return (
    <div
      className="relative w-screen"
      style={{ minHeight: '100vh', color: COLORS.textPrimary, overflowX: 'hidden' }}
    >
      {/* Animated cyber background (behind content). */}
      <FxBackdrop variant="page" />

      {/* Top bar: brand on the left, language + sign-in + create-ID on the right. */}
      <header
        className="absolute top-0 left-0 right-0 flex items-center justify-between px-6 py-4"
        style={{ zIndex: 20 }}
      >
        <div
          className="font-display fx-neon"
          style={{ ...FONT.title, fontSize: 18, color: COLORS.textPrimary, ['--fx-c' as string]: COLORS.accent }}
        >
          {t('landing.brand')}
        </div>
        <div className="flex items-center gap-3">
          <LanguageSwitcher />
          <button
            type="button"
            onClick={() => {
              setRegisterOpen(false);
              setSignInOpen((v) => !v);
            }}
            className="px-3 py-2 font-semibold"
            style={{
              backgroundColor: 'transparent',
              color: COLORS.textPrimary,
              border: `1px solid ${COLORS.border}`,
              borderRadius: RADIUS.card,
            }}
          >
            {t('landing.signIn')}
          </button>
          <button
            type="button"
            onClick={() => {
              setSignInOpen(false);
              setRegisterOpen(true);
            }}
            className="px-3 py-2 font-semibold"
            style={{ backgroundColor: COLORS.accent, color: '#FFFFFF', borderRadius: RADIUS.card }}
          >
            {t('landing.createId')}
          </button>
        </div>
      </header>

      {/* Hero + feature cards */}
      <main
        className="relative z-10 flex flex-col items-center justify-center px-6 text-center"
        style={{ minHeight: '100vh', paddingTop: 96, paddingBottom: 56 }}
      >
        <div
          className="w-full flex flex-col md:flex-row items-center justify-center gap-8"
          style={{ maxWidth: 1080 }}
        >
          {/* Left: copy */}
          <div className="flex-1 min-w-0 text-center md:text-left">
            <p
              className="font-mono-x fx-enter fx-enter-1"
              style={{ ...FONT.metric, color: COLORS.textSecondary, letterSpacing: 2 }}
            >
              {t('landing.tagline')}
            </p>
            <h1
              className="font-display fx-shimmer fx-enter fx-enter-2"
              style={{ fontSize: 40, fontWeight: 800, maxWidth: 820, margin: '12px 0', lineHeight: 1.15, color: COLORS.textPrimary }}
            >
              {t('landing.heroTitle')}
            </h1>
            <p className="fx-enter fx-enter-3" style={{ ...FONT.metric, color: COLORS.textSecondary, maxWidth: 660, margin: '0 auto' }}>
              {t('landing.heroDesc')}
            </p>

            <div className="fx-enter fx-enter-4 mt-8 flex items-center justify-center md:justify-start gap-3">
              <button
                type="button"
                onClick={() => setSignInOpen(true)}
                className="px-5 py-2.5 font-semibold fx-lift"
                style={{ backgroundColor: COLORS.accent, color: '#FFFFFF', borderRadius: RADIUS.card }}
              >
                {t('landing.signIn')}
              </button>
              <button
                type="button"
                onClick={() => setRegisterOpen(true)}
                className="px-5 py-2.5 font-semibold fx-lift"
                style={{
                  backgroundColor: 'transparent',
                  color: COLORS.textPrimary,
                  border: `1px solid ${COLORS.border}`,
                  borderRadius: RADIUS.card,
                }}
              >
                {t('landing.createId')}
              </button>
            </div>
          </div>

          {/* Right: floating robot */}
          <div className="fx-enter fx-enter-3 flex items-center justify-center">
            <div className="fx-float">
              <RobotAvatar variant="humanoid" state="online" size={220} animated glow />
            </div>
          </div>
        </div>

        {/* Feature cards */}
        <div
          className="mt-12 grid gap-4"
          style={{
            gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
            maxWidth: 920,
            width: '100%',
          }}
        >
          {features.map((f, i) => (
            <div
              key={f.title}
              className={`p-5 text-left fx-lift fx-enter fx-enter-${i + 5}`}
              style={{ backgroundColor: COLORS.surface, border: `1px solid ${COLORS.border}`, borderRadius: RADIUS.card }}
            >
              <div style={{ ...FONT.title, fontSize: 15, color: COLORS.textPrimary }}>{f.title}</div>
              <div style={{ ...FONT.metric, color: COLORS.textSecondary, marginTop: 6 }}>{f.desc}</div>
            </div>
          ))}
        </div>
      </main>

      {/* Corner sign-in flyout (top-right). */}
      {signInOpen && (
        <div
          className="absolute"
          style={{
            top: 72,
            right: 24,
            zIndex: 30,
            width: 320,
            backgroundColor: COLORS.surface,
            border: `1px solid ${COLORS.border}`,
            borderRadius: RADIUS.card,
            padding: 20,
          }}
        >
          <div className="flex items-center justify-between mb-3">
            <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('landing.loginHint')}</span>
            <button
              type="button"
              aria-label="Close"
              onClick={() => setSignInOpen(false)}
              style={{
                background: 'transparent',
                color: COLORS.textSecondary,
                border: 'none',
                cursor: 'pointer',
                fontSize: 18,
                lineHeight: 1,
              }}
            >
              ×
            </button>
          </div>
          <LoginForm autoFocus />
        </div>
      )}

      {/* Registration modal (lazy-loaded chunk). */}
      {registerOpen && (
        <div
          className="fixed inset-0 flex items-center justify-center"
          style={{ backgroundColor: COLORS.overlay, zIndex: 40 }}
          onClick={() => setRegisterOpen(false)}
        >
          <div
            className="p-6"
            style={{ backgroundColor: COLORS.surface, border: `1px solid ${COLORS.border}`, borderRadius: RADIUS.card }}
            onClick={(e) => e.stopPropagation()}
          >
            <Suspense
              fallback={
                <div style={{ ...FONT.metric, color: COLORS.textSecondary, padding: 24 }}>…</div>
              }
            >
              <RegisterForm onBackToSignIn={() => setRegisterOpen(false)} />
            </Suspense>
          </div>
        </div>
      )}
    </div>
  );
}
