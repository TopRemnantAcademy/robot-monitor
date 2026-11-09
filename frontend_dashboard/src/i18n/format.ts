// Locale-aware clock formatter. Replaces the previously hard-coded `zh-CN`
// formatting so times render in the user's chosen language.

import type { Locale } from './locales';

const INTL_LOCALE: Record<Locale, string> = {
  en: 'en-GB',
  ko: 'ko-KR',
  zh: 'zh-CN',
  ja: 'ja-JP',
};

export function formatClock(ms: number, locale: Locale): string {
  return new Date(ms).toLocaleTimeString(INTL_LOCALE[locale], { hour12: false });
}
