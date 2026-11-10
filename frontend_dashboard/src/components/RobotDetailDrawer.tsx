// spec/03 §5.3 —— single-robot joint heatmap diagnostics Drawer (slides in from right, 480px, doesn't squeeze the main area)
//
// Data sources:
//   1) Real-time increment: taken directly from Zustand's robots Map (this robot updates at 10Hz)
//   2) History line chart: GET {VITE_API_BASE}/robots/{id}/history?minutes=5&resolution=10s (spec/00 §5.3)

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
// Order 09: use echarts-for-react's core entry + on-demand registered echarts instance,
// replacing the default entry (the default entry internally requires the entire echarts package, can't tree-shake).
// Registration list see src/lib/echartsCore.ts (only contains the line-chart-related modules used by this Drawer).
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../lib/echartsCore';
import JointHeatmap from './JointHeatmap';
import {
  COLORS,
  DISPLAY_COLOR,
  FONT,
  hasEverReported,
  robotDisplayState,
  severityColor,
} from '../theme/tokens';
import { useRobotStore } from '../store/robotStore';
import { JOINT_NAMES } from '../types/robot';
import type { AlertApiItem, HistoryPoint } from '../types/robot';
import { RULE_KEY } from './AlertLogTable';
import { ackAlert, fetchAlerts, unackAlert } from '../auth/alertsApi';
import { useIsAdmin } from '../auth/authStore';
import { useI18n } from '../i18n/I18nProvider';
import { formatClock } from '../i18n/format';
import {
  HISTORY_MINUTES,
  HISTORY_RESOLUTION,
  HISTORY_WINDOW_OPTIONS,
  fetchRobotHistory,
  robotHistoryKey,
} from '../api/robotHistory';

// Order 26: Drawer no longer holds its own REST base URL, uniformly taken from auth/api (re-export to keep existing references compatible).
import { API_BASE } from '../auth/api';

export { API_BASE };

/**
 * "Last updated" millisecond timestamp: prefer the backend's `last_seen` (receiver-side fact, seconds → milliseconds),
 * fall back to the robot's self-reported `ts` (milliseconds) only when missing. Neither → null (shows "--").
 */
function lastSeenMs(robot: { last_seen?: number | null; ts: number } | undefined): number | null {
  if (!robot) return null;
  if (typeof robot.last_seen === 'number') return robot.last_seen * 1000;
  return robot.ts > 0 ? robot.ts : null;
}

/** "Data frame age": duration from last_seen to now (used for lost-contact diagnosis — "3 hours ago" is more intuitive than a moment number). */
function frameAge(ms: number | null, t: (k: string, v?: Record<string, string | number>) => string): string {
  if (ms === null) return '--';
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (sec < 60) return t('frameAge.sec', { n: sec });
  const min = Math.floor(sec / 60);
  if (min < 60) return t('frameAge.min', { n: min });
  return t('frameAge.hour', { n: Math.floor(min / 60) });
}

interface ChartSeries {
  name: string;
  data: (number | null)[];
}

interface ChartModel {
  xAxis: string[];
  series: ChartSeries[];
  /** Battery per bucket (HistoryPointOut.battery_pct), for the standalone battery trend chart */
  battery: (number | null)[];
}

/** Line colors: temp=fault red / torque=accent blue / angle=normal green / envelope=warning yellow (all tokens, no self-created hues). */
const SERIES_COLORS = [COLORS.statusFault, COLORS.accent, COLORS.statusNormal, COLORS.statusWarning];

export default function RobotDetailDrawer() {
  const { t, locale } = useI18n();
  const selectedRobotId = useRobotStore((s) => s.selectedRobotId);
  const selectRobot = useRobotStore((s) => s.selectRobot);
  // ISSUE #75 structural sharing: Drawer only subscribes to **the cell it displays**
  // (`s.robots.get(selectedRobotId)`), no longer subscribes to the whole Map —— otherwise while the Drawer is closed
  // every 100ms store-flush frame would also re-render it once. The selector auto-shifts on selection change / close.
  const robot = useRobotStore((s) => (s.selectedRobotId ? s.robots.get(s.selectedRobotId) : undefined));

  const [selectedJoint, setSelectedJoint] = useState<string>(JOINT_NAMES[0]);
  // Order 27 T3: history window (minutes). Goes into queryKey —— switching windows changes the key, react-query auto refetches.
  const [historyMinutes, setHistoryMinutes] = useState<number>(HISTORY_MINUTES);
  // Order 27 T2: alert acknowledge (admin only). busyId and error message only apply to this Drawer's alert area.
  const [ackBusyId, setAckBusyId] = useState<number | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const canAck = useIsAdmin();

  // Reset selected joint each time the Drawer opens, to avoid leftover selection from the previous robot
  useEffect(() => {
    if (selectedRobotId) setSelectedJoint(JOINT_NAMES[0]);
  }, [selectedRobotId]);

  // Order 26 (I-20): history query delegated to react-query for caching and retry.
  //   - Reopen same robot: hits cache within staleTime, no backend call;
  //   - Auto-retry on failure (except 4xx, see lib/queryClient.ts);
  //   - Switching robot / closing Drawer: react-query auto-cancels in-flight requests (signal passed through to fetch).
  const historyQuery = useQuery({
    queryKey: robotHistoryKey(selectedRobotId ?? '', historyMinutes),
    queryFn: ({ signal }) => fetchRobotHistory(selectedRobotId as string, signal, historyMinutes),
    enabled: selectedRobotId !== null,
  });

  const history: HistoryPoint[] = historyQuery.data ?? [];
  // When enabled=false (no robot selected), isPending is always true but no request was sent, so use fetchStatus to distinguish.
  const loading = historyQuery.isPending && historyQuery.fetchStatus !== 'idle';
  const error = historyQuery.isError
    ? historyQuery.error instanceof Error
      ? historyQuery.error.message
      : t('error.load')
    : null;

  // ECharts line model: temp/torque/angle three series, x-axis is 10s bucket time
  const chart = useMemo<ChartModel>(() => {
    const xAxis: string[] = [];
    const temp: (number | null)[] = [];
    const torque: (number | null)[] = [];
    const position: (number | null)[] = [];
    const maxTemp: (number | null)[] = [];
    const battery: (number | null)[] = [];

    for (const point of history) {
      const t = new Date(point.time);
      xAxis.push(
        `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}:${String(t.getSeconds()).padStart(2, '0')}`,
      );
      const tv = point.joint_temperatures?.[selectedJoint];
      const tq = point.joint_torques?.[selectedJoint];
      const po = point.joint_positions?.[selectedJoint];
      temp.push(typeof tv === 'number' ? Number(tv.toFixed(2)) : null);
      torque.push(typeof tq === 'number' ? Number(tq.toFixed(2)) : null);
      position.push(typeof po === 'number' ? Number(po.toFixed(3)) : null);
      // Whole-robot max-temperature envelope (backend aggregates max_joint_temp per bucket, not client-computed):
      // looking only at the selected joint misses "other joints running hot" — envelope rising means a whole-robot heat problem.
      maxTemp.push(
        typeof point.max_joint_temp === 'number' ? Number(point.max_joint_temp.toFixed(2)) : null,
      );
      battery.push(typeof point.battery_pct === 'number' ? Number(point.battery_pct.toFixed(2)) : null);
    }
    return {
      xAxis,
      series: [
        { name: t('chart.temp'), data: temp },
        { name: t('chart.torque'), data: torque },
        { name: t('chart.angle'), data: position },
        { name: t('chart.maxTemp'), data: maxTemp },
      ],
      battery,
    };
  }, [history, selectedJoint, t]);

  // This robot's recent alerts: REST is the authoritative view (includes post-hoc ack/escalated status, WS only pushes increments).
  // The backend /alerts has no robot_id query param yet, so fetch the latest 200 (_MAX_LIMIT=500 range) and filter client-side.
  //
  // Order 35: this query **includes** suppressed items (`suppressed` deliberately not sent).
  // Different from AlertLogTable's default exclusion, the reason is this area's purpose ——
  // it's "when troubleshooting, see what this robot actually reported", the suppressed rows are exactly the batch most needing to be seen
  // (they explain "why didn't this pop up"). So here we want the full set, and mark "suppressed" inline.
  const alertsQuery = useQuery({
    queryKey: ['robot-alerts', selectedRobotId],
    queryFn: () => fetchAlerts({ limit: 200 }),
    enabled: selectedRobotId !== null,
  });
  const robotAlerts: AlertApiItem[] = useMemo(() => {
    const items = alertsQuery.data?.items;
    if (!Array.isArray(items)) return [];
    return items.filter((a) => a.robot_id === selectedRobotId).slice(0, 5);
  }, [alertsQuery.data, selectedRobotId]);

  // Order 27 T1: 24-joint real-time torque load row model (pure CSS bars, no new ECharts instance).
  // maxAbs takes the max |torque| of joints with values in the current frame as full scale; clamp to 1 when all-zero/no data to prevent divide-by-zero,
  // at which point all bar widths are 0 —— the semantics are still "no load" rather than an error.
  const torqueRows = useMemo(() => {
    const torques = robot?.joint_torques ?? {};
    let maxAbs = 0;
    for (const j of JOINT_NAMES) {
      const v = torques[j];
      if (typeof v === 'number') maxAbs = Math.max(maxAbs, Math.abs(v));
    }
    if (maxAbs < 1e-9) maxAbs = 1;
    return JOINT_NAMES.map((joint) => {
      const v = torques[joint];
      const value = typeof v === 'number' ? v : null;
      return {
        joint,
        value,
        pct: value === null ? 0 : Math.min(100, (Math.abs(value) / maxAbs) * 100),
      };
    });
  }, [robot?.joint_torques]);

  // Order 27 T2: acknowledge/withdraw (admin only visible, 403 covered by backend —— frontend just doesn't show the unclickable thing).
  // On success invalidate this Drawer's alert query: REST is the authoritative view of ack state, re-fetch reconciles.
  const handleToggleAck = useCallback(
    async (a: AlertApiItem) => {
      setAckBusyId(a.alert_id);
      setAckError(null);
      try {
        await (a.ack ? unackAlert(a.alert_id) : ackAlert(a.alert_id));
        await queryClient.invalidateQueries({ queryKey: ['robot-alerts', selectedRobotId] });
      } catch (e) {
        // Deliberately don't pass through the original text containing "HTTP 5xx": in the 500 test case getByText(/HTTP 500/)
        // must uniquely hit the history-chart error (test contract), and 5xx has no operational value for the operator.
        const msg = e instanceof Error ? e.message : t('error.op');
        setAckError(msg.includes('HTTP 5') ? t('ack.error') : msg);
      } finally {
        setAckBusyId(null);
      }
    },
    [queryClient, selectedRobotId, t],
  );

  if (!selectedRobotId) return null;

  const chartOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: {
      data: chart.series.map((s) => s.name),
      textStyle: { color: COLORS.textSecondary, fontSize: 11 },
      top: 0,
    },
    grid: { left: 44, right: 16, top: 32, bottom: 24 },
    xAxis: {
      type: 'category',
      data: chart.xAxis,
      axisLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLine: { lineStyle: { color: COLORS.border } },
      splitLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    series: chart.series.map((s, idx) => ({
      name: s.name,
      type: 'line' as const,
      smooth: true,
      showSymbol: chart.xAxis.length <= 2,
      connectNulls: true,
      data: s.data,
      lineStyle: { width: 2, color: SERIES_COLORS[idx] },
      itemStyle: { color: SERIES_COLORS[idx] },
    })),
    animation: false,
  };

  // Battery trend: the card wall / basic info only have instantaneous battery, rate anomalies like "lost >10% in 5 minutes" are visible at a glance here.
  // Data is the backend's per-bucket aggregated battery_pct (HistoryPointOut), not client-computed.
  const batteryChartOption = {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 36, right: 12, top: 10, bottom: 22 },
    xAxis: {
      type: 'category',
      data: chart.xAxis,
      axisLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { lineStyle: { color: COLORS.border } },
      splitLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
    },
    series: [
      {
        name: t('chart.battery'),
        type: 'line' as const,
        smooth: true,
        showSymbol: chart.xAxis.length <= 2,
        connectNulls: true,
        data: chart.battery,
        lineStyle: { width: 2, color: COLORS.accent },
        itemStyle: { color: COLORS.accent },
        // 12% transparent derivation of accent #3B82F6 (same derivation as tokens.overlay, no new hue introduced)
        areaStyle: { color: 'rgba(59, 130, 246, 0.12)' },
      },
    ],
    animation: false,
  };

  // ISSUE #77: the Drawer header's status also only recognizes is_online.
  // The old `STATUS_COLOR[robot.status_code]` would keep a stopped-reporting "warning" robot
  // yellow in the Drawer —— contradicting the card wall's "lost contact" gray.
  const displayState = robot
    ? robotDisplayState(robot.is_online === true, robot.status_code, hasEverReported(robot))
    : 'offline';
  const statusColor = robot ? DISPLAY_COLOR[displayState] : COLORS.statusOffline;
  const statusText = robot ? t(`display.${displayState}`) : t('display.noData');

  return (
    <>
      {/* Mask layer: click to close (doesn't block the main area layout, Drawer is fixed-positioned) */}
      <div
        className="fixed inset-0 z-30"
        style={{ backgroundColor: COLORS.overlay }}
        onClick={() => selectRobot(null)}
      />
      <aside
        className="fixed right-0 top-0 h-screen z-40 flex flex-col overflow-hidden"
        style={{
          width: '480px',
          backgroundColor: COLORS.surface,
          borderLeft: `1px solid ${COLORS.border}`,
          borderRadius: 0,
        }}
      >
        <div
          className="h-14 shrink-0 flex items-center justify-between px-4"
          style={{ borderBottom: `1px solid ${COLORS.border}` }}
        >
          <div className="flex items-baseline gap-2 min-w-0">
            <span style={{ ...FONT.title, color: COLORS.textPrimary }}>{selectedRobotId}</span>
            <span style={{ ...FONT.metric, color: statusColor }}>{statusText}</span>
          </div>
          <button
            type="button"
            onClick={() => selectRobot(null)}
            className="px-2 py-1 cursor-pointer"
            style={{ ...FONT.metric, color: COLORS.textSecondary, border: `1px solid ${COLORS.border}` }}
          >
            {t('drawer.close')}
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 flex flex-col gap-4">
          {/* Basic info */}
          <section>
            <h3 style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.title')}</h3>
            <div className="grid grid-cols-3 gap-2 mt-1">
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.battery')}</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot ? `${robot.battery_pct.toFixed(1)}%` : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.maxJointTemp')}</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot && robot.is_online === true ? `${robot.max_joint_temp.toFixed(1)}°C` : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                {/*
                  "Last updated" prefers the backend's last_seen (**the moment we received** that frame),
                  rather than the robot's self-reported ts: for offline diagnosis the latter is the robot's word (possibly clock drift,
                  or computed by itself), the former is the physical evidence sitting on the server side (ISSUE #77).
                  When both exist, prefer last_seen; when the old backend only gives ts, fall back, don't show blank.
                */}
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.lastUpdate')}</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {lastSeenMs(robot) !== null
                    ? formatClock(lastSeenMs(robot) as number, locale)
                    : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.statusCode')}</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {robot ? robot.status_code : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.errorCode')}</span>
                <span
                  className="tabular-nums"
                  style={{
                    color:
                      robot && robot.error_code !== 0 ? COLORS.statusFault : COLORS.textPrimary,
                    fontWeight: 600,
                  }}
                >
                  {robot ? robot.error_code : '--'}
                </span>
              </div>
              <div className="flex flex-col">
                <span style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('info.frameAge')}</span>
                <span className="tabular-nums" style={{ color: COLORS.textPrimary, fontWeight: 600 }}>
                  {frameAge(lastSeenMs(robot), t)}
                </span>
              </div>
            </div>
          </section>

          {/* Joint temperature heatmap array (24 joints) */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              {t('joint.arrayTitle')}
            </h3>
            <JointHeatmap
              temperatures={robot?.joint_temperatures ?? {}}
              selectedJoint={selectedJoint}
              onSelectJoint={setSelectedJoint}
            />
          </section>

          {/* Joint torque load (real-time frame snapshot, pure CSS bars —— no new ECharts instance to preserve bundle size.
              Click to switch selected joint, linked with the heatmap; aria-label starts with "torque ",
              distinct from the heatmap joint block's aria-label (starting with joint name) on the accessibility tree). */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              {t('torque.title')}
            </h3>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-1">
              {torqueRows.map(({ joint, value, pct }) => (
                <button
                  key={joint}
                  type="button"
                  aria-label={value === null ? t('torque.ariaNoData', { joint }) : t('torque.aria', { joint, val: value.toFixed(2) })}
                  onClick={() => setSelectedJoint(joint)}
                  className="flex items-center gap-1.5 min-w-0 cursor-pointer"
                  style={{
                    border: `1px solid ${joint === selectedJoint ? COLORS.accent : 'transparent'}`,
                    borderRadius: 2,
                    padding: '1px 2px',
                    backgroundColor: 'transparent',
                  }}
                >
                  <span
                    className="truncate shrink-0"
                    style={{ ...FONT.metric, color: COLORS.textSecondary, maxWidth: 104 }}
                  >
                    {joint}
                  </span>
                  <span
                    className="flex-1 h-1.5 min-w-0"
                    style={{ backgroundColor: COLORS.border, borderRadius: 1, overflow: 'hidden' }}
                  >
                    <span
                      className="block h-full"
                      style={{ width: `${pct}%`, backgroundColor: COLORS.accent }}
                    />
                  </span>
                  <span
                    className="tabular-nums shrink-0"
                    style={{ ...FONT.metric, color: COLORS.textPrimary }}
                  >
                    {value === null ? '--' : value.toFixed(2)}
                  </span>
                </button>
              ))}
            </div>
          </section>

          {/* Selected joint's history line chart (window switchable: 5/30/60m, switching auto-refetches via queryKey) */}
          <section>
            <div className="flex items-center justify-between gap-2 mb-1">
              <h3 style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {t('history.title', { joint: selectedJoint, m: historyMinutes, res: HISTORY_RESOLUTION })}
              </h3>
              <div className="flex items-center gap-1 shrink-0">
                {HISTORY_WINDOW_OPTIONS.map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setHistoryMinutes(m)}
                    className="px-1.5 py-0.5 rounded cursor-pointer"
                    style={{
                      fontSize: '10px',
                      fontWeight: 600,
                      color: historyMinutes === m ? COLORS.bgBase : COLORS.textSecondary,
                      backgroundColor: historyMinutes === m ? COLORS.accent : 'transparent',
                      border: `1px solid ${historyMinutes === m ? COLORS.accent : COLORS.border}`,
                    }}
                  >
                    {m}m
                  </button>
                ))}
              </div>
            </div>
            {loading ? (
              <div className="h-56 flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {t('history.loading')}
              </div>
            ) : error ? (
              <div className="h-56 flex items-center justify-center text-center px-4" style={{ ...FONT.metric, color: COLORS.statusFault }}>
                {t('history.unavailable', { error, api: API_BASE, id: selectedRobotId })}
              </div>
            ) : chart.xAxis.length === 0 ? (
              <div className="h-56 flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {t('history.noPoints', { m: historyMinutes })}
              </div>
            ) : (
              <ReactEChartsCore
                echarts={echarts}
                option={chartOption}
                style={{ height: 224, width: '100%' }}
                notMerge
                lazyUpdate
              />
            )}
          </section>

          {/* Battery trend (backend per-bucket aggregated value). Only mounted when history loaded successfully ——
              empty/error text appears only once in the joint-chart block above, avoiding dual ambiguity in tests and visuals. */}
          {!loading && !error && chart.xAxis.length > 0 ? (
            <section>
              <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                {t('battery.title', { m: historyMinutes })}
              </h3>
              <ReactEChartsCore
                echarts={echarts}
                option={batteryChartOption}
                style={{ height: 112, width: '100%' }}
                notMerge
                lazyUpdate
              />
            </section>
          ) : null}

          {/* This robot's recent alerts (REST authoritative view, filter latest 200 by robot_id take first 5;
              Order 27 T2: admin can acknowledge/withdraw in place, invalidate and re-fetch on success to reconcile) */}
          <section>
            <h3 className="mb-1" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
              {t('alerts.title')}
            </h3>
            {ackError ? (
              <div className="mb-1" style={{ ...FONT.metric, color: COLORS.statusFault }}>
                {ackError}
              </div>
            ) : null}
            {alertsQuery.isPending && alertsQuery.fetchStatus !== 'idle' ? (
              <div style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('alerts.loading')}</div>
            ) : alertsQuery.isError ? (
              // Deliberately without HTTP code: in the 500 test case getByText(/HTTP 500/) must uniquely hit the history-chart error
              <div style={{ ...FONT.metric, color: COLORS.statusFault }}>{t('alerts.loadFail')}</div>
            ) : robotAlerts.length === 0 ? (
              <div style={{ ...FONT.metric, color: COLORS.textSecondary }}>{t('alerts.empty')}</div>
            ) : (
              <ul className="flex flex-col gap-1.5">
                {robotAlerts.map((a) => (
                  <li key={a.alert_id} className="flex items-center gap-2 min-w-0">
                    <span
                      className="w-2 h-2 rounded-full shrink-0"
                      style={{ backgroundColor: severityColor(a.severity) }}
                    />
                    <span
                      className="tabular-nums shrink-0"
                      style={{ ...FONT.metric, color: COLORS.textSecondary }}
                    >
                      {formatClock(new Date(a.time).getTime(), locale)}
                    </span>
                    <span className="shrink-0" style={{ ...FONT.metric, color: severityColor(a.severity) }}>
                      {t(RULE_KEY[a.rule_id] ?? a.rule_id)}
                    </span>
                    <span className="truncate" style={{ ...FONT.metric, color: COLORS.textPrimary }} title={a.message}>
                      {a.message}
                    </span>
                    {a.escalated ? (
                      <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.statusWarning }}>
                        {t('alerts.escalated')}
                      </span>
                    ) : null}
                    {a.suppressed ? (
                      // Order 35: same wording and same color token as AlertLogTable (textSecondary,
                      // not statusFault —— suppression is not a new fault). title gives the root-cause rule,
                      // so "why didn't this pop up" can be answered in this area.
                      <span
                        className="shrink-0"
                        style={{ ...FONT.metric, color: COLORS.textSecondary }}
                        title={
                          a.suppressed_by
                            ? t('suppressed.title', { rule: t(RULE_KEY[a.suppressed_by] ?? a.suppressed_by) })
                            : t('suppressed.titleSimple')
                        }
                      >
                        ⊘ {t('alerts.suppressed')}
                      </span>
                    ) : null}
                    {a.ack ? (
                      <span className="shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
                        {t('alerts.acked')}
                      </span>
                    ) : null}
                    {canAck ? (
                      <button
                        type="button"
                        disabled={ackBusyId === a.alert_id}
                        onClick={() => void handleToggleAck(a)}
                        className="shrink-0 px-2 py-0.5 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          fontSize: '10px',
                          fontWeight: 600,
                          color: COLORS.textPrimary,
                          backgroundColor: COLORS.surface,
                          border: `1px solid ${COLORS.border}`,
                        }}
                        title={a.ack ? t('ack.titleUnack') : t('ack.titleAck')}
                      >
                        {a.ack ? t('alerts.unack') : t('alerts.ack')}
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </aside>
    </>
  );
}
