// R-VLA-EVAL Phase 1: visible proof that the VLA policy-action stream reaches the browser.
//
// The WS hub coalesces the latest policy-action frame per robot and broadcasts it as
// `{"type":"action","data":{...}}` (see backend app/ws_hub.py). This panel subscribes to
// the store's `actions` map (one entry per robot) and renders the most recent frame, so a
// reviewer can confirm "动作帧渲染" end-to-end without waiting for the Phase 4 Trials view.
//
// Layout note: kept `shrink-0` + a bounded inner scroll so it does not break the
// spec/03 §4 "no page-level scrolling" rule.

import { useMemo } from 'react';
import { COLORS, FONT } from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { useI18n } from '../i18n/I18nProvider';

function fmtTs(ts: number | null): string {
  if (ts == null) return '—';
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

export default function PolicyActionStream() {
  const { t } = useI18n();
  const actions = useRobotStore((s) => s.actions);

  // Subscribe to the map reference; it only changes when a flush committed new frames.
  const rows = useMemo(() => Array.from(actions.values()), [actions]);

  return (
    <section
      className="shrink-0 flex flex-col"
      style={{ backgroundColor: COLORS.surface, borderTop: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-center justify-between px-4 py-2">
        <span style={{ ...FONT.title, color: COLORS.textPrimary }}>{t('action.header')}</span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          {rows.length} · {t('action.live')}
        </span>
      </div>

      <div className="overflow-y-auto px-4 pb-3" style={{ maxHeight: 148 }}>
        {rows.length === 0 ? (
          <p style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('action.noData')}</p>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
            {rows.map((a) => (
              <div
                key={a.robot_id}
                className="p-2 rounded"
                style={{ backgroundColor: COLORS.bgBase, border: `1px solid ${COLORS.border}` }}
              >
                <div className="flex items-center justify-between">
                  <span style={{ ...FONT.metric, color: COLORS.textPrimary, fontWeight: 600 }}>
                    {a.robot_id}
                  </span>
                  <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{fmtTs(a.ts)}</span>
                </div>
                <div style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                  {t('action.policy')}:{' '}
                  <span style={{ color: COLORS.textPrimary }}>{a.policy_version || '—'}</span>
                </div>
                <div className="truncate" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                  {t('action.vector')}:{' '}
                  <span style={{ color: COLORS.accent }}>
                    [
                    {a.action
                      .slice(0, 8)
                      .map((x) => x.toFixed(2))
                      .join(', ')}
                    {a.action.length > 8 ? '…' : ''}]
                  </span>
                </div>
                <div className="truncate" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                  {t('action.trial')}:{' '}
                  <span style={{ color: COLORS.textPrimary }}>{a.trial_id ?? '—'}</span>
                  {' · '}
                  {t('action.goal')}:{' '}
                  <span style={{ color: COLORS.textPrimary }}>{a.goal ?? '—'}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
