// Supported-model catalog (UI + admin UI role-aware).
//
// - viewer: model cards (name / form factor / DOF / registered count / online count / note).
// - admin : same cards + fleet-management detail (vendor / firmware versions / stations / unregistered flag).
//
// Data comes from GET /supported-robots (DB-driven: distinct robot_metadata.model_type
// + fleet composition). Colors / font sizes all come from theme/tokens — no new hues (spec/03 §3).
//
// Layout: this component is a `shrink-0` horizontal strip (fixed height, internal horizontal
// scroll), inserted between FleetHealthAnalytics and FleetOverviewCards; it does not consume
// the flex-1 space of the card wall.

import { useQuery } from '@tanstack/react-query';
import { COLORS, FONT, RADIUS } from '../theme/tokens';
import { fetchSupportedRobots } from '../auth/api';
import { useIsAdmin } from '../auth/authStore';
import { supportedRobotsKey, type SupportedRobot } from '../types/robot';
import { useI18n } from '../i18n/I18nProvider';

export default function SupportedRobotsPanel() {
  const { t } = useI18n();
  const isAdmin = useIsAdmin();
  const { data, isLoading, isError } = useQuery({
    queryKey: supportedRobotsKey,
    queryFn: fetchSupportedRobots,
    staleTime: 60_000,
  });

  const headerRight = data
    ? t('sr.headerRight', { n: data.models.length, m: data.total_robots })
    : isLoading
      ? t('sr.loading')
      : '—';

  return (
    <section
      className="shrink-0 px-3 py-2"
      style={{ backgroundColor: COLORS.bgBase, borderBottom: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span style={{ ...FONT.title, fontSize: '14px', color: COLORS.textPrimary }}>
          {t('sr.header')}
        </span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{headerRight}</span>
        {isAdmin && (
          <span
            className="ml-auto px-1.5 py-0.5 rounded-sm"
            style={{
              fontSize: '10px',
              color: COLORS.accent,
              border: `1px solid ${COLORS.accent}`,
            }}
          >
            {t('sr.adminView')}
          </span>
        )}
      </div>

      {isError ? (
        <span style={{ ...FONT.metric, color: COLORS.statusFault }}>{t('sr.loadFail')}</span>
      ) : (
        <div className="flex gap-2 overflow-x-auto pb-1">
          {(data?.models ?? []).map((m) => (
            <ModelCard key={m.model_type} model={m} isAdmin={isAdmin} />
          ))}
          {isLoading && !data && (
            <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('sr.loading')}</span>
          )}
        </div>
      )}
    </section>
  );
}

function ModelCard({ model, isAdmin }: { model: SupportedRobot; isAdmin: boolean }) {
  const { t } = useI18n();
  const allOnline = model.total > 0 && model.online === model.total;
  const onlineColor = allOnline ? COLORS.statusNormal : COLORS.statusWarning;

  return (
    <div
      className="shrink-0 flex flex-col gap-1 px-3 py-2"
      style={{
        minWidth: '208px',
        backgroundColor: COLORS.surface,
        border: `1px solid ${COLORS.border}`,
        borderRadius: RADIUS.card,
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <span style={{ fontSize: '13px', fontWeight: 700, color: COLORS.textPrimary }}>
          {model.display_name}
        </span>
        {model.generic && (
          <span style={{ fontSize: '9px', color: COLORS.textSecondary }}>{t('sr.unregistered')}</span>
        )}
      </div>

      <span style={{ fontSize: '10px', color: COLORS.textSecondary }}>{model.model_type}</span>

      <div className="flex flex-wrap gap-1">
        <Tag label={model.form_factor === '—' ? t('sr.formFactorUndefined') : model.form_factor} />
        <Tag label={model.dof > 0 ? t('sr.dof', { n: model.dof }) : t('sr.dofUndefined')} />
      </div>

      <div className="flex items-center gap-2" style={{ fontSize: '11px' }}>
        <span style={{ color: COLORS.textSecondary }}>{t('sr.registered')}</span>
        <span className="tabular-nums" style={{ color: COLORS.textPrimary }}>
          {model.total}
        </span>
        <span style={{ color: COLORS.textSecondary }}>{t('sr.online')}</span>
        <span className="tabular-nums" style={{ color: onlineColor }}>
          {model.online}
        </span>
      </div>

      {isAdmin && (
        <div
          className="flex flex-col gap-0.5 mt-0.5 pt-1"
          style={{
            borderTop: `1px solid ${COLORS.border}`,
            fontSize: '10px',
            color: COLORS.textSecondary,
          }}
        >
          <span>{t('sr.vendor', { v: model.vendor })}</span>
          <span>
            {t('sr.firmware', {
              f: model.firmware_versions.length ? model.firmware_versions.join(' / ') : '—',
            })}
          </span>
          <span>{t('sr.stations', { n: model.stations })}</span>
        </div>
      )}

      <span
        style={{ fontSize: '10px', color: COLORS.textSecondary, lineHeight: 1.4 }}
      >
        {model.description}
      </span>
    </div>
  );
}

function Tag({ label }: { label: string }) {
  return (
    <span
      className="px-1.5 py-0.5 rounded-sm"
      style={{
        fontSize: '10px',
        color: COLORS.textSecondary,
        backgroundColor: COLORS.bgBase,
        border: `1px solid ${COLORS.border}`,
      }}
    >
      {label}
    </span>
  );
}
