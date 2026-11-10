// GitHub issue #7 — theme switching (dark / light).
//
// The UI never imports a palette directly beyond `COLORS`/`DISPLAY_COLOR`/`STATUS_COLOR` (which are
// theme-aware Proxies over `PALETTES` in tokens.ts). This provider is the single place that decides which
// palette is active and publishes it to the DOM. On toggle it flips `document.documentElement.dataset.theme`
// (so index.css can reskin the page background / scrollbars) and remounts its subtree via `key`, forcing every
// `COLORS.x` consumer to re-read the new palette.

import { cloneElement, createContext, useContext, useEffect, useMemo, useState, type ReactElement, type ReactNode } from 'react';
import { setActiveTheme, type ThemeName } from './tokens';

const STORAGE_KEY = 'rm-theme';

interface ThemeContextValue {
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;
  cycleTheme: () => void;
}

// Default context: lets components call `useTheme()` without a provider (e.g. isolated unit tests)
// by returning the dark theme and no-op setters. The real ThemeProvider overrides this.
const defaultContext: ThemeContextValue = {
  theme: 'dark',
  setTheme: () => {},
  cycleTheme: () => {},
};
const ThemeContext = createContext<ThemeContextValue>(defaultContext);

/** Resolve the initial theme: explicit saved choice wins; otherwise follow the OS preference. */
function resolveInitialTheme(): ThemeName {
  if (typeof window === 'undefined') return 'dark';
  const saved = window.localStorage.getItem(STORAGE_KEY);
  if (saved === 'light' || saved === 'dark') return saved;
  const prefersLight = window.matchMedia?.('(prefers-color-scheme: light)').matches ?? false;
  return prefersLight ? 'light' : 'dark';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(() => {
    const initial = resolveInitialTheme();
    // Publish synchronously so the very first paint already uses the resolved palette (no dark flash).
    if (typeof document !== 'undefined') {
      setActiveTheme(initial);
      document.documentElement.dataset.theme = initial;
    }
    return initial;
  });

  // Keep the DOM attribute + localStorage in sync on every change.
  useEffect(() => {
    setActiveTheme(theme);
    if (typeof document !== 'undefined') document.documentElement.dataset.theme = theme;
    if (typeof window !== 'undefined') window.localStorage.setItem(STORAGE_KEY, theme);
  }, [theme]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      theme,
      setTheme: (t: ThemeName) => {
        setActiveTheme(t); // keep the Proxy in lock-step so the remounted subtree reads the new values immediately
        setThemeState(t);
      },
      cycleTheme: () =>
        setThemeState((prev) => {
          const next: ThemeName = prev === 'dark' ? 'light' : 'dark';
          setActiveTheme(next);
          return next;
        }),
    }),
    [theme],
  );

  // Remount the subtree on theme change so every `COLORS.x` consumer re-reads the active palette.
  return (
    <ThemeContext.Provider value={value}>
      {cloneElement(children as ReactElement, { key: theme })}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
