// FleetHealthAnalytics component test (vitest + testing-library).
//
// Mirrors the canonical template src/components/__tests__/RobotDetailDrawer.test.tsx.
// This component renders 4 ECharts panels (status / battery / temp distribution + online-rate/alert trend)
// from the shared zustand store. jsdom has no canvas, so we mock `echarts-for-react/lib/core` and dump the
// `option` it receives into a DOM data-testid — letting us assert the "chart model computed by the component"
// (the pure distribution/trend logic) without drawing pixels.
//
// We drive the store through its real API (applyTelemetryBatch / pushFleetSample / selectStatusCounts) and assert:
//   - the 4 panels are present, the 3 distribution panels render their mocked echarts-option, and the seeded
//     distribution values match (status=battery=temp buckets reflect the data);
//   - after pushing a fleet sample (interaction), the trend panel's echarts-option appears and reflects the sample;
//   - changing the robots re-derives the status distribution (interaction).

import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import FleetHealthAnalytics from '../FleetHealthAnalytics';
import {
  buildRobotIds,
  offlinePlaceholder,
  useRobotStore,
  type FleetSample,
} from '../../store/robotStore';
import type { RobotTelemetry } from '../../types/robot';

// —— ECharts double: serialize option into DOM for assertions ——
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}));

/** Reset the store to a fresh 100-robot skeleton of offline placeholders before each case. */
function resetStore() {
  const robots = new Map<string, RobotTelemetry>();
  for (const id of buildRobotIds(100)) robots.set(id, offlinePlaceholder(id));
  useRobotStore.setState({
    robots,
    selectedRobotId: null,
    fleetSamples: [],
    alerts: [],
  });
}

/** Seed all 100 robots as online with given battery/temp. */
function seedAllOnline(battery: number, temp: number) {
  const now = Math.floor(Date.now() / 1000);
  const list = buildRobotIds(100).map((id) => ({
    ...offlinePlaceholder(id),
    status_code: 1,
    is_online: true,
    last_seen: now,
    battery_pct: battery,
    max_joint_temp: temp,
  })) as RobotTelemetry[];
  useRobotStore.getState().applyTelemetryBatch(list);
}

interface ChartSeries {
  series: { data: (number | { value: number })[] }[];
}
function readOption(index: number): ChartSeries {
  const els = screen.getAllByTestId('echarts-option');
  return JSON.parse(els[index]?.textContent ?? '{}') as ChartSeries;
}
function bucketValues(option: ChartSeries): number[] {
  return option.series[0].data.map((d) =>
    typeof d === 'number' ? d : d.value,
  );
}

beforeEach(() => {
  vi.useFakeTimers(); // prevent the component's 2s fleet sampler from auto-firing mid-test
  resetStore();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('FleetHealthAnalytics', () => {
  it('Renders the 4 panels and 3 distribution charts (trend still accumulating with no samples)', () => {
    seedAllOnline(80, 30);
    render(<FleetHealthAnalytics />);

    // All four panel titles present.
    expect(screen.getByText('Status distribution (units)')).toBeInTheDocument();
    expect(screen.getByText('Battery distribution (%, reported)')).toBeInTheDocument();
    expect(screen.getByText('Joint temp distribution (°C, online)')).toBeInTheDocument();
    expect(screen.getByText('Online rate / active alerts trend (3 min)')).toBeInTheDocument();

    // 3 distribution charts rendered; trend panel shows the accumulating placeholder until samples arrive.
    expect(screen.getAllByTestId('echarts-option')).toHaveLength(3);
    expect(screen.getByText(/Trend accumulating/)).toBeInTheDocument();
  });

  it('Distribution values match the seeded fleet (all online: status=100 online, temp<40, battery 80) ', () => {
    seedAllOnline(80, 30);
    render(<FleetHealthAnalytics />);

    // DOM order: [0]=status, [1]=battery, [2]=temp
    const status = bucketValues(readOption(0));
    const battery = bucketValues(readOption(1));
    const temp = bucketValues(readOption(2));

    // All 100 online → status bucket [online,warning,fault,stale,offline] = [100,0,0,0,0].
    expect(status).toEqual([100, 0, 0, 0, 0]);
    // Battery 80 → 80-100 bucket (last) = 100.
    expect(battery).toEqual([0, 0, 0, 0, 100]);
    // Temp 30 → <40 bucket (first) = 100.
    expect(temp).toEqual([100, 0, 0, 0, 0]);
    // Buckets are exhaustive: each sums to 100.
    expect(status.reduce((a, b) => a + b, 0)).toBe(100);
  });

  it('Pushing a fleet sample makes the trend chart appear and reflect the sample', () => {
    seedAllOnline(80, 30);
    render(<FleetHealthAnalytics />);
    expect(screen.getAllByTestId('echarts-option')).toHaveLength(3); // trend not yet rendered

    const sample: FleetSample = {
      t: 1_700_000_000_000,
      onlineRate: 100,
      activeAlerts: 3,
      avgBattery: 80,
      avgTemp: 30,
    };
    act(() => {
      useRobotStore.getState().pushFleetSample(sample);
    });

    // Trend panel now renders a 4th chart.
    expect(screen.getAllByTestId('echarts-option')).toHaveLength(4);
    const trend = readOption(3);
    // First series = online rate, second = active alerts.
    expect(trend.series[0].data).toEqual([100]);
    expect(trend.series[1].data).toEqual([3]);
  });

  it('Changing robots re-derives the status distribution', () => {
    seedAllOnline(80, 30);
    render(<FleetHealthAnalytics />);
    expect(bucketValues(readOption(0))).toEqual([100, 0, 0, 0, 0]);

    // Flip all 100 to offline (stop reporting). New objects → reference change → re-render.
    const now = Math.floor(Date.now() / 1000);
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(
        buildRobotIds(100).map((id) => ({
          ...offlinePlaceholder(id),
          is_online: false,
          last_seen: now, // makes them "stale" (lost) rather than "never"
        })) as RobotTelemetry[],
      );
    });

    // All 100 now stale → status bucket [online,warning,fault,stale,offline] = [0,0,0,100,0].
    expect(bucketValues(readOption(0))).toEqual([0, 0, 0, 100, 0]);
  });
});
