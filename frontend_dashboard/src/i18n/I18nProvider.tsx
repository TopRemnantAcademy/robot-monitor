// React context for the active locale. Default locale is English; the choice is
// persisted to localStorage so it survives reloads.
//
// `useI18n` returns a safe fallback when no provider is mounted (e.g. unit tests
// that render a component in isolation) so callers never have to wrap every test.

import { createContext, useCallback, useContext, useState, type ReactNode } from 'react';
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY, translate, type Locale } from './locales';

interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

// Stable fallback identity used when no <I18nProvider> is mounted (e.g. unit tests
// that render a single component without the full provider tree). These MUST be
// module-level constants — NOT recreated on every useI18n() call. Otherwise `t`
// gets a new reference each render, which destabilises any useCallback/useEffect
// that lists `t` in its deps (e.g. AlertLogTable's `load` + `useEffect([load])`),
// producing an infinite async re-render loop that hangs the test runner.
const FALLBACK_LOCALE: Locale = DEFAULT_LOCALE;
const FALLBACK_SET_LOCALE = () => {};
const FALLBACK_T: I18nContextValue['t'] = (key, vars) => translate(DEFAULT_LOCALE, key, vars);

function readInitialLocale(): Locale {
  if (typeof localStorage === 'undefined') return DEFAULT_LOCALE;
  const saved = localStorage.getItem(LOCALE_STORAGE_KEY);
  if (saved === 'en' || saved === 'ko' || saved === 'zh') return saved;
  return DEFAULT_LOCALE;
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readInitialLocale);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(LOCALE_STORAGE_KEY, next);
    }
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => translate(locale, key, vars),
    [locale],
  );

  return <I18nContext.Provider value={{ locale, setLocale, t }}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    return { locale: FALLBACK_LOCALE, setLocale: FALLBACK_SET_LOCALE, t: FALLBACK_T };
  }
  return ctx;
}
