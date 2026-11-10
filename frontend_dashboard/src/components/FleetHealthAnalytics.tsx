// Fleet health analytics strip (UI enhancement) — the "see the whole plant at a glance" layer below the top KPI bar.
//
// Why added: the original dashboard top bar only had 3 scalars (online rate / alert count / average battery),
// and the card wall is 100 single cards, missing a layer of **fleet-level distribution and trends**: e.g.
// "30 robots with joint temp >70°C at the same time" or "battery collectively dropping below 30%" —
// such cross-robot common problems can't be seen in scalars or single cards; they must be aggregated into histograms/trends.
//
// Performance guardrails (AGENTS.md §3 / ISSUE #75 / #77, hard constraints for this component):
//   - The distribution chart uses a **bucket-count selector** wrapped in useShallow: only re-renders when some bucket count changes,
//     never subscribes to the entire robots Map (otherwise every 100ms store flush frame would re-render the whole analytics strip).
//   - The trend line uses the store's low-frequency sampling ring (fleetSamples, one point per 2s, see robotStore.computeFleetSample),
//     decoupled from the 100ms hot path.
//   - Bucketing is O(n) (n=100) running once per 100ms frame = only ~1k ops/sec, negligible.
//
// Color discipline (spec/03 §3 "no self-changing colors"): all colors come from theme/tokens, no new hues introduced;
// battery/temp bar coloring directly reuses batteryColor / tempColor, same color scale as the card wall and Drawer.
//
// Bundle discipline (Issue #62): only uses echarts' already-registered BarChart / LineChart (echartsCore.ts),
// no new registrations, no importing the full echarts.

import { useEffect, type ReactNode } from 'react';
import { useShallow } from 'zustand/react/shallow';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../lib/echartsCore';
import {
  COLORS,
  DISPLAY_COLOR,
  FONT,
  batteryColor,
  hasEverReported,
  robotDisplayState,
  tempColor,
} from '../theme/tokens';
import type { RobotDisplayState } from '../theme/tokens';
import { computeFleetSample, useRobotStore } from '../store/robotStore';
import { useI18n } from '../i18n/I18nProvider';

/** Sampling interval (ms). 2s decouples from the 100ms store flush, and 90 points ≈ a 3-minute window — enough to see trends. */
const SAMPLE_INTERVAL_MS = 2000;

/** Fixed display-state order (aligned with DISPLAY_COLOR/DISPLAY_LABEL; the index is the bucket number). */
const STATUS_ORDER: RobotDisplayState[] = ['online', 'warning', 'fault', 'stale', 'offline'];

/** Battery histogram bucket boundaries and labels (right-open intervals; the last bucket includes 100). */
const BATTERY_BUCKETS = [
  { max: 20, label: '0-20' },
  { max: 40, label: '20-40' },
  { max: 60, label: '40-60' },
  { max: 80, label: '60-80' },
  { max: Infinity, label: '80-100' },
] as const;

/** Temperature histogram bucket boundaries and labels. */
const TEMP_BUCKETS = [
  { max: 40, label: '<40' },
  { max: 50, label: '40-50' },
  { max: 60, label: '50-60' },
  { max: 70, label: '60-70' },
  { max: Infinity, label: '≥70' },
] as const;

/** Bucketing: returns the count falling into each bucket (last bucket catches the remainder). */
function bucketize(values: number[], bounds: readonly { max: number }[]): number[] {
  const counts = new Array(bounds.length).fill(0);
  for (const v of values) {
    for (let i = 0; i < bounds.length; i += 1) {
      if (v < bounds[i].max) {
        counts[i] += 1;
        break;
      }
    }
  }
  return counts;
}

function timeLabel(ms: number): string {
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Single chart panel: title + chart, with uniform style and height. */
function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div
      className="flex flex-col min-w-[220px] flex-1 min-h-0 rounded-lg overflow-hidden"
      style={{ backgroundColor: COLORS.surface, border: `1px solid ${COLORS.border}` }}
    >
      <span className="px-2.5 pt-1.5 pb-1 shrink-0" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
        {title}
      </span>
      <div className="flex-1 min-h-0">{children}</div>
    </div>
  );
}

const CHART_STYLE = { height: 92, width: '100%' } as const;

export default function FleetHealthAnalytics() {
  const { t } = useI18n();
  // —— Distribution: bucket-count selector, useShallow compares element-by-element, no re-render if buckets unchanged ——
  const statusDist = useRobotStore(
    useShallow((s) => {
      const counts = new Array(STATUS_ORDER.length).fill(0) as number[];
      for (const r of s.robots.values()) {
        const st = robotDisplayState(r.is_online === true, r.status_code, hasEverReported(r));
        counts[STATUS_ORDER.indexOf(st)] += 1;
      }
      return counts;
    }),
  );

  // Battery distribution only counts robots that **have reported telemetry** (unreported placeholder 0% would pollute the 0-20 bucket up to 100).
  const batteryDist = useRobotStore(
    useShallow((s) => {
      const vals: number[] = [];
      for (const r of s.robots.values()) if (hasEverReported(r)) vals.push(r.battery_pct);
      return bucketize(vals, BATTERY_BUCKETS);
    }),
  );

  // Temperature distribution only counts online robots (offline/lost-contact temps are residual ghosts and shouldn't enter the heatmap).
  const tempDist = useRobotStore(
    useShallow((s) => {
      const vals: number[] = [];
      for (const r of s.robots.values()) if (r.is_online === true) vals.push(r.max_joint_temp);
      return bucketize(vals, TEMP_BUCKETS);
    }),
  );

  // —— Trend: subscribe to the low-frequency sampling ring (one point per 2s) ——
  const samples = useRobotStore((s) => s.fleetSamples);
  const pushFleetSample = useRobotStore((s) => s.pushFleetSample);

  // Sampler: mounted on this component (Dashboard resident), stops on unmount. Uses getState() for a snapshot, no extra subscription.
  useEffect(() => {
    const id = window.setInterval(() => {
      pushFleetSample(computeFleetSample(useRobotStore.getState()));
    }, SAMPLE_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [pushFleetSample]);

  const axisLabel = { color: COLORS.textSecondary, fontSize: 9 };
  const splitLine = { lineStyle: { color: COLORS.border } };

  // ① Status distribution: horizontal bars, one color per state + count label on the right.
  const statusOption = {
    backgroundColor: 'transparent',
    grid: { left: 4, right: 30, top: 4, bottom: 4, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', minInterval: 1, axisLabel, splitLine },
      yAxis: {
        type: 'category',
        inverse: true,
        data: STATUS_ORDER.map((st) => t(`display.${st}`)),
        axisLabel: { color: COLORS.textSecondary, fontSize: 10 },
        axisLine: { lineStyle: { color: COLORS.border } },
      },
    series: [
      {
        type: 'bar',
        barWidth: 9,
        data: STATUS_ORDER.map((st, i) => ({
          value: statusDist[i] ?? 0,
          itemStyle: { color: DISPLAY_COLOR[st], borderRadius: [0, 3, 3, 0] },
        })),
        label: { show: true, position: 'right', color: COLORS.textPrimary, fontSize: 10 },
      },
    ],
    animation: false,
  };

  // ② Battery histogram: vertical bars, colored by battery color scale (same as card battery bar).
  const batteryOption = {
    backgroundColor: 'transparent',
    grid: { left: 30, right: 8, top: 10, bottom: 16 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: BATTERY_BUCKETS.map((b) => b.label),
      axisLabel,
      axisLine: { lineStyle: { color: COLORS.border } },
    },
    yAxis: { type: 'value', minInterval: 1, axisLabel, splitLine },
    series: [
      {
        type: 'bar',
        barWidth: '55%',
        data: batteryDist.map((v, i) => ({
          value: v,
          itemStyle: { color: batteryColor([10, 30, 50, 70, 92][i]), borderRadius: [3, 3, 0, 0] },
        })),
      },
    ],
    animation: false,
  };

  // ③ Temperature histogram: vertical bars, colored by temperature color scale (same as card temperature number).
  const tempOption = {
    backgroundColor: 'transparent',
    grid: { left: 30, right: 8, top: 10, bottom: 16 },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category',
      data: TEMP_BUCKETS.map((b) => b.label),
      axisLabel,
      axisLine: { lineStyle: { color: COLORS.border } },
    },
    yAxis: { type: 'value', minInterval: 1, axisLabel, splitLine },
    series: [
      {
        type: 'bar',
        barWidth: '55%',
        data: tempDist.map((v, i) => ({
          value: v,
          itemStyle: { color: tempColor([35, 45, 55, 65, 75][i]), borderRadius: [3, 3, 0, 0] },
        })),
      },
    ],
    animation: false,
  };

  // ④ Online rate / active alerts trend: dual Y-axis line chart (0–100 left axis; alert count right axis).
  const trendOption = {
    backgroundColor: 'transparent',
    grid: { left: 32, right: 32, top: 22, bottom: 16 },
    tooltip: { trigger: 'axis' },
    legend: {
      data: [t('legend.onlineRate'), t('legend.activeAlerts')],
      textStyle: { color: COLORS.textSecondary, fontSize: 10 },
      top: 0,
      itemWidth: 12,
      itemHeight: 8,
    },
    xAxis: {
      type: 'category',
      data: samples.map((s) => timeLabel(s.t)),
      axisLabel,
      axisLine: { lineStyle: { color: COLORS.border } },
    },
    yAxis: [
      { type: 'value', min: 0, max: 100, axisLabel, splitLine },
      { type: 'value', min: 0, axisLabel, splitLine: { show: false } },
    ],
    series: [
      {
        name: 'Online rate %',
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: samples.map((s) => s.onlineRate),
        lineStyle: { width: 2, color: COLORS.statusNormal },
        itemStyle: { color: COLORS.statusNormal },
      },
      {
        name: 'Active alerts',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        data: samples.map((s) => s.activeAlerts),
        lineStyle: { width: 2, color: COLORS.statusFault },
        itemStyle: { color: COLORS.statusFault },
      },
    ],
    animation: false,
  };

  return (
    <div className="h-[132px] shrink-0 flex gap-3 px-3 py-2 overflow-x-auto">
      <Panel title={t('panel.statusDist')}>
        <ReactEChartsCore echarts={echarts} option={statusOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title={t('panel.batteryDist')}>
        <ReactEChartsCore echarts={echarts} option={batteryOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title={t('panel.tempDist')}>
        <ReactEChartsCore echarts={echarts} option={tempOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title={t('panel.trend')}>
        {samples.length === 0 ? (
          <div className="h-full flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            {t('trend.accumulating', { n: SAMPLE_INTERVAL_MS / 1000 })}
          </div>
        ) : (
          <ReactEChartsCore echarts={echarts} option={trendOption} style={CHART_STYLE} notMerge lazyUpdate />
        )}
      </Panel>
    </div>
  );
}
