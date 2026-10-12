// 机群健康分析条（UI 强化）—— 顶栏 KPI 之下的「一眼看全厂」层。
//
// 为什么加：原大屏顶栏只有 3 个标量（在线率/告警数/平均电量），卡片墙是 100 张单卡，
// 缺一层**机群级分布与趋势**：比如「同时有 30 台关节温度 >70°C」「电量集体掉到 30% 以下」
// 这种跨机器人的共性问题，在标量和单卡上都看不出来，必须聚合成直方图/趋势才可见。
//
// 性能护栏（AGENTS.md §3 / ISSUE #75 / #77，本组件的硬约束）：
//   - 分布图用 useShallow 包裹的**分桶计数选择器**：只在某个桶计数变化时才重渲染，
//     绝不订阅整张 robots Map（否则每个 100ms 刷库帧都重渲染整条分析条）。
//   - 趋势线用 store 的低频采样环（fleetSamples，2s 一点，见 robotStore.computeFleetSample），
//     与 100ms 热路径解耦。
//   - 分桶 O(n)（n=100）在 100ms 帧上跑一次也仅 ~1k 次/秒运算，可忽略。
//
// 配色纪律（spec/03 §3「禁止自改色」）：全部色值取自 theme/tokens，不引入新色相；
// 电量/温度柱的着色直接复用 batteryColor / tempColor，与卡片墙、Drawer 同一套色阶。
//
// Bundle 纪律（Issue #62）：只用到 echarts 已注册的 BarChart / LineChart（echartsCore.ts），
// 不新增注册项，不 import 全量 echarts。

import { useEffect, type ReactNode } from 'react';
import { useShallow } from 'zustand/react/shallow';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from '../lib/echartsCore';
import {
  COLORS,
  DISPLAY_COLOR,
  DISPLAY_LABEL,
  FONT,
  batteryColor,
  hasEverReported,
  robotDisplayState,
  tempColor,
} from '../theme/tokens';
import type { RobotDisplayState } from '../theme/tokens';
import { computeFleetSample, useRobotStore } from '../store/robotStore';

/** 采样间隔（ms）。2s 与 100ms 刷库解耦，且 90 点 ≈ 3 分钟窗口，够看趋势。 */
const SAMPLE_INTERVAL_MS = 2000;

/** 展示态固定顺序（与 DISPLAY_COLOR/DISPLAY_LABEL 对齐，索引即桶号）。 */
const STATUS_ORDER: RobotDisplayState[] = ['online', 'warning', 'fault', 'stale', 'offline'];

/** 电量直方图桶边界与标签（右开区间，末桶含 100）。 */
const BATTERY_BUCKETS = [
  { max: 20, label: '0-20' },
  { max: 40, label: '20-40' },
  { max: 60, label: '40-60' },
  { max: 80, label: '60-80' },
  { max: Infinity, label: '80-100' },
] as const;

/** 温度直方图桶边界与标签。 */
const TEMP_BUCKETS = [
  { max: 40, label: '<40' },
  { max: 50, label: '40-50' },
  { max: 60, label: '50-60' },
  { max: 70, label: '60-70' },
  { max: Infinity, label: '≥70' },
] as const;

/** 分桶：返回落入每个桶的计数（末桶兜底）。 */
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

/** 单个图表面板：标题 + 图表，统一样式与高度。 */
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
  // —— 分布：分桶计数选择器，useShallow 逐元素比较，桶不变则不重渲染 ——
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

  // 电量分布只统计**收到过遥测**的机器人（未上报的占位 0% 会把 0-20 桶污染成 100）。
  const batteryDist = useRobotStore(
    useShallow((s) => {
      const vals: number[] = [];
      for (const r of s.robots.values()) if (hasEverReported(r)) vals.push(r.battery_pct);
      return bucketize(vals, BATTERY_BUCKETS);
    }),
  );

  // 温度分布只统计在线机器人（离线/失联的温度是残影，不该进热力图）。
  const tempDist = useRobotStore(
    useShallow((s) => {
      const vals: number[] = [];
      for (const r of s.robots.values()) if (r.is_online === true) vals.push(r.max_joint_temp);
      return bucketize(vals, TEMP_BUCKETS);
    }),
  );

  // —— 趋势：订阅低频采样环（2s 一点）——
  const samples = useRobotStore((s) => s.fleetSamples);
  const pushFleetSample = useRobotStore((s) => s.pushFleetSample);

  // 采样器：挂在本组件（Dashboard 常驻），卸载即停。用 getState() 取快照，不额外订阅。
  useEffect(() => {
    const id = window.setInterval(() => {
      pushFleetSample(computeFleetSample(useRobotStore.getState()));
    }, SAMPLE_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [pushFleetSample]);

  const axisLabel = { color: COLORS.textSecondary, fontSize: 9 };
  const splitLine = { lineStyle: { color: COLORS.border } };

  // ① 状态分布：横向条形，每态一色 + 右侧计数标签。
  const statusOption = {
    backgroundColor: 'transparent',
    grid: { left: 4, right: 30, top: 4, bottom: 4, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', minInterval: 1, axisLabel, splitLine },
    yAxis: {
      type: 'category',
      inverse: true,
      data: STATUS_ORDER.map((st) => DISPLAY_LABEL[st]),
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

  // ② 电量直方图：竖柱，按电量色阶着色（与卡片电池条同色）。
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

  // ③ 温度直方图：竖柱，按温度色阶着色（与卡片温度数字同色）。
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

  // ④ 在线率 / 活跃告警趋势：双 Y 轴折线（0–100 左轴；告警计数右轴）。
  const trendOption = {
    backgroundColor: 'transparent',
    grid: { left: 32, right: 32, top: 22, bottom: 16 },
    tooltip: { trigger: 'axis' },
    legend: {
      data: ['在线率%', '活跃告警'],
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
        name: '在线率%',
        type: 'line',
        smooth: true,
        showSymbol: false,
        data: samples.map((s) => s.onlineRate),
        lineStyle: { width: 2, color: COLORS.statusNormal },
        itemStyle: { color: COLORS.statusNormal },
      },
      {
        name: '活跃告警',
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
      <Panel title="状态分布（台）">
        <ReactEChartsCore echarts={echarts} option={statusOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title="电量分布（%，收到过遥测）">
        <ReactEChartsCore echarts={echarts} option={batteryOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title="关节温度分布（°C，在线）">
        <ReactEChartsCore echarts={echarts} option={tempOption} style={CHART_STYLE} notMerge lazyUpdate />
      </Panel>
      <Panel title="在线率 / 活跃告警趋势（3 分钟）">
        {samples.length === 0 ? (
          <div className="h-full flex items-center justify-center" style={{ ...FONT.metric, color: COLORS.textSecondary }}>
            趋势积累中…（每 {SAMPLE_INTERVAL_MS / 1000}s 采样）
          </div>
        ) : (
          <ReactEChartsCore echarts={echarts} option={trendOption} style={CHART_STYLE} notMerge lazyUpdate />
        )}
      </Panel>
    </div>
  );
}
