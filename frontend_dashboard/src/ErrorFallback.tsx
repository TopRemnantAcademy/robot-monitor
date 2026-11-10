// Order 26 (I-20): fallback UI for the global error boundary.
//
// Positioning: **last line of defense**. When a component render throws, the entire subtree is unmounted and the user sees this instead of a white screen.
// The dashboard is an "unattended" scenario —— a broken-but-lit screen is better than all black, so two actions are given:
//   1) "Retry" resetErrorBoundary(): first re-mount the subtree inside the boundary (most transient errors are recoverable);
//   2) "Reload page": also resets state outside the boundary (such as WS), more thorough fallback.
// Styling reuses design tokens (spec/03 §3), no new color values introduced.

import type { FallbackProps } from 'react-error-boundary';
import { COLORS, FONT } from './theme/tokens';
import { useI18n } from './i18n/I18nProvider';

export default function ErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  const { t } = useI18n();
  const message = error instanceof Error ? error.message : String(error);

  return (
    <div
      className="h-screen w-screen flex flex-col items-center justify-center gap-4 px-6 text-center"
      style={{ backgroundColor: COLORS.bgBase, color: COLORS.textPrimary }}
      role="alert"
    >
      <h1 style={{ ...FONT.title, color: COLORS.statusFault }}>{t('error.title')}</h1>
      <p style={{ ...FONT.metric, color: COLORS.textSecondary, maxWidth: 640 }}>
        {t('error.desc')}
      </p>
      <pre
        className="overflow-auto px-3 py-2"
        style={{
          ...FONT.metric,
          color: COLORS.textSecondary,
          backgroundColor: COLORS.surface,
          border: `1px solid ${COLORS.border}`,
          maxWidth: 720,
          maxHeight: 160,
          whiteSpace: 'pre-wrap',
          textAlign: 'left',
        }}
      >
        {message}
      </pre>
      <div className="flex gap-3">
        <button
          type="button"
          onClick={resetErrorBoundary}
          className="px-3 py-1 cursor-pointer"
          style={{ ...FONT.metric, color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
        >
          {t('error.retry')}
        </button>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="px-3 py-1 cursor-pointer"
          style={{ ...FONT.metric, color: COLORS.textPrimary, border: `1px solid ${COLORS.border}` }}
        >
          {t('error.reload')}
        </button>
      </div>
    </div>
  );
}
