// Tests for the theme system (GitHub issue #7).
//
// 1) tokens: `COLORS` is a Proxy that resolves against the active palette — no call-site edits needed.
// 2) ThemeProvider: publishes <html data-theme>, respects localStorage fallback, and toggling updates the
//    attribute + the rendered palette (the dashboard remounts via key, so consumers re-read the new values).

import { describe, expect, it } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { ThemeProvider, useTheme } from '../ThemeProvider';
import { COLORS, PALETTES, setActiveTheme } from '../tokens';

function ThemeReadout() {
  const { theme, cycleTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme-name">{theme}</span>
      <span data-testid="bg-base">{COLORS.bgBase}</span>
      <span data-testid="text-primary">{COLORS.textPrimary}</span>
      <button type="button" onClick={cycleTheme}>
        cycle
      </button>
    </div>
  );
}

describe('theme tokens (Proxy over palettes)', () => {
  it('COLORS resolves to the active palette and switches with setActiveTheme', () => {
    setActiveTheme('dark');
    expect(COLORS.bgBase).toBe(PALETTES.dark.bgBase);
    expect(COLORS.textPrimary).toBe(PALETTES.dark.textPrimary);

    setActiveTheme('light');
    expect(COLORS.bgBase).toBe(PALETTES.light.bgBase);
    expect(COLORS.textPrimary).toBe(PALETTES.light.textPrimary);

    // leave the module in a known state for other tests
    setActiveTheme('dark');
  });
});

describe('ThemeProvider', () => {
  it('defaults to dark (no saved pref) and publishes data-theme + the rendered palette', () => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-theme');

    render(
      <ThemeProvider>
        <ThemeReadout />
      </ThemeProvider>,
    );

    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(screen.getByTestId('bg-base').textContent).toBe(PALETTES.dark.bgBase);
    expect(screen.getByTestId('text-primary').textContent).toBe(PALETTES.dark.textPrimary);
  });

  it('toggling the theme updates data-theme and the rendered palette (remount re-reads COLORS)', () => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-theme');

    render(
      <ThemeProvider>
        <ThemeReadout />
      </ThemeProvider>,
    );

    act(() => {
      screen.getByText('cycle').click();
    });

    expect(document.documentElement.dataset.theme).toBe('light');
    expect(screen.getByTestId('theme-name').textContent).toBe('light');
    expect(screen.getByTestId('bg-base').textContent).toBe(PALETTES.light.bgBase);
    expect(screen.getByTestId('text-primary').textContent).toBe(PALETTES.light.textPrimary);
  });
});
