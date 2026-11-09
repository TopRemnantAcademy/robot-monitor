// Language switcher shown in the top bar. Persists the choice via I18nProvider.

import { COLORS, FONT } from '../theme/tokens';
import { LOCALES, type Locale } from './locales';
import { useI18n } from './I18nProvider';

export default function LanguageSwitcher() {
  const { locale, setLocale } = useI18n();
  return (
    <select
      aria-label="Language"
      value={locale}
      onChange={(e) => setLocale(e.target.value as Locale)}
      style={{
        ...FONT.metric,
        backgroundColor: COLORS.bgBase,
        color: COLORS.textPrimary,
        border: `1px solid ${COLORS.border}`,
        borderRadius: 4,
        padding: '2px 4px',
      }}
    >
      {LOCALES.map((l) => (
        <option key={l.code} value={l.code}>
          {l.label}
        </option>
      ))}
    </select>
  );
}
