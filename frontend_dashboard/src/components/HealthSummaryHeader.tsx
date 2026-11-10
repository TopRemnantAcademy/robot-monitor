// spec/03 §5.2 —— top-bar three KPIs (online rate / current alert count / average battery) + WS connection status dot

import { useMemo } from 'react';
import { COLORS, FONT } from '../theme/tokens';
import { selectStatusCounts, useRobotStore } from '../store/robotStore';
import { useShallow } from 'zustand/react/shallow';
import { useI18n } from '../i18n/I18nProvider';
import LanguageSwitcher from '../i18n/LanguageSwitcher';

function Kpi({ label, value, unit, color }: { label: string; value: string; unit?: string; color?: string }) {
  return (
    <div className="flex flex-col items-end min-w-[110px]">
      <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{label}</span>
      <span className="tabular-nums" style={{ fontSize: '18px', fontWeight: 700, color: color ?? COLORS.textPrimary }}>
        {value}
        {unit ? <span style={{ ...FONT.metric, marginLeft: 2 }}>{unit}</span> : null}
      </span>
    </div>
  );
}

export default function HealthSummaryHeader() {
  const { t } = useI18n();
  const alerts = useRobotStore((s) => s.alerts);
  const wsStatus = useRobotStore((s) => s.wsStatus);

  // ISSUE #75 structural sharing: no longer subscribe to the whole robots Map (every 100ms store-flush frame would make
  // the top bar re-render once). Switch to a **derived stats selector** wrapped in useShallow —— output is shallow-compared per field,
  // only re-renders when "the value actually displayed on screen" changes:
  //   - onlineRate/avgBattery rounded to 1 decimal (consistent with .toFixed(1) display precision,
  //     otherwise pure float jitter like 57.03→57.06 would also penetrate the shallow compare and trigger meaningless re-renders);
  //   - online/total/stale are themselves low-frequency integers;
  //   - alerts / wsStatus keep their own single-field subscriptions (low-frequency, and useShallow gives no benefit for arrays/strings).
  // selectStatusCounts receives { robots } rather than a closure Map —— keeps the same data-fetch path as the original implementation.
  const stats = useRobotStore(
    useShallow((s) => {
      const counts = selectStatusCounts(s);
      let batterySum = 0;
      let batteryCount = 0;
      // ISSUE #77: online count and average battery come **from the backend's is_online**,
      // no longer self-counted with `status_code !== 0` —— the latter permanently counts stopped-reporting robots as online.
      for (const r of s.robots.values()) {
        if (r.is_online === true) {
          batterySum += r.battery_pct;
          batteryCount += 1;
        }
      }
      // online rate = online count / 100 (spec/03 §5.2), offline/lost-contact robots not counted in average battery
      return {
        onlineRate: counts.total === 0 ? 0 : Math.round((counts.online / counts.total) * 1000) / 10,
        online: counts.online,
        total: counts.total,
        stale: counts.stale,
        avgBattery: batteryCount === 0 ? 0 : Math.round((batterySum / batteryCount) * 10) / 10,
      };
    }),
  );

  // Current alert count = alerts with severity ≥ 2 (spec/03 §5.2; un-acked semantics computed streamingly on the frontend)
  const activeAlerts = useMemo(() => alerts.filter((a) => a.severity >= 2).length, [alerts]);

  const dotColor =
    wsStatus === 'open' ? COLORS.statusNormal : wsStatus === 'connecting' ? COLORS.statusWarning : COLORS.statusFault;
  const dotLabel =
    wsStatus === 'open' ? t('ws.connected') : wsStatus === 'connecting' ? t('ws.connecting') : t('ws.reconnecting');

  return (
    <header
      className="h-14 shrink-0 flex items-center justify-between px-4"
      style={{ backgroundColor: COLORS.surface, borderBottom: `1px solid ${COLORS.border}` }}
    >
      <div className="flex items-baseline gap-2">
        <span style={{ ...FONT.title, color: COLORS.textPrimary }}>RobotMonitor</span>
        <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>
          {t('header.subtitle')}
        </span>
      </div>

      <div className="flex items-center gap-6">
        <Kpi
          label={t('kpi.onlineRate')}
          value={stats.onlineRate.toFixed(1)}
          // The lost-contact count is shown directly in parentheses: looking at the percentage alone you can't tell "1 dropped or 40 dropped",
          // and the handling actions for the two are completely different (the former goes on-site, the latter first checks the Redis/MQTT link).
          unit={`% (${stats.online}/${stats.total}${stats.stale > 0 ? t('unit.lost', { n: stats.stale }) : ''})`}
          color={stats.onlineRate >= 90 ? COLORS.statusNormal : COLORS.statusWarning}
        />
        <Kpi
          label={t('kpi.alerts')}
          value={String(activeAlerts)}
          color={activeAlerts > 0 ? COLORS.statusFault : COLORS.textPrimary}
        />
        <Kpi label={t('kpi.avgBattery')} value={stats.avgBattery.toFixed(1)} unit="%" />

        <LanguageSwitcher />

        <div className="flex items-center gap-2">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{ backgroundColor: dotColor, boxShadow: `0 0 6px ${dotColor}` }}
          />
          <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>WS {dotLabel}</span>
        </div>
      </div>
    </header>
  );
}
