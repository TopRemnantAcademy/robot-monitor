// Order 33 T2 —— fleet render guard regression test (100-card wall + health analytics strip).
//
// 【Which hole this test patches】
// AGENTS.md §3 frontend guardrail says "strictly forbid each WS message from triggering a React re-render; must throttle at 100ms".
// Order 16 T2 already delivered the **online counter** (src/lib/observability.ts) —— but that's "run it and eyeball
// the console" observation, with **no automated assertion at all**. So whoever someday changes the card's self-subscription back to subscribing to the whole Map,
// or removes FleetHealthAnalytics's useShallow, `npm test` would still be all green, until the dashboard freezes
// and someone notices. This file nails that guardrail into a decidable red/green (Order 33's "conjecture: currently no test asserts
// that the 100-card wall's render count doesn't explode linearly with fleet size" is confirmed true by this order; patched here).
//
// 【Choice of measurement instrument —— the part this file most needs to explain】
// Tried but **abandoned** Profiler: React.Profiler's onRender only reports "commits of the wrapped subtree",
// measured "3 changed" and "100 changed" frames have **identical** Profiler callback counts (both 10,
// once per frame) —— it can't count "how many cards re-rendered". Using it would make the threshold meaningless (self-deception).
//
// Instruments adopted (two, each precise):
//   1) Card-level: `vi.mock('../theme/tokens')` swaps `robotDisplayState` for a spy.
//      RobotCard's function body calls it **exactly once per render** (FleetOverviewCards.tsx:37),
//      so spy call count == card render count, precise to 1. On mount = 100 (100 cards).
//   2) Grid-body level: observability's `cardRenders` (Order 16 T2) ——
//      it self-increments in the **RobotGrid body** (FleetOverviewCards.tsx:116-118), counting "how many times the grid
//      container re-rendered", decoupled from per-card rendering. The two complement each other, neither can be omitted.
//
// 【Threshold basis —— not pulled out of thin air】
// 100 units × 10Hz = 1000 msg/s, compressed by useRobotWebSocket's 100ms throttle into 10 frames/s.
// In one frame, "how many actually changed" (k) decides the **ideal** render ceiling, independent of fleet size N:
//   · Idle frame (this frame's entries share references with state): applyTelemetryBatch detects no reference change,
//     returns the original state directly → zustand zero notification → cards 0 times, grid body 0 times re-render.
//   · k-changed frame: cards re-render exactly k times (self-subscribe s.robots.get(id), move only when reference changes);
//     grid body 0 times (it only subscribes to selectedRobotId and selectRobot, both unchanged this frame).
// So assertions use **equality/constants** rather than "loose less-than":
//   changed=k, frames=f  ⇒  card render increment == k × f (existing implementation measured exact hit, see T1 conclusion);
//                         grid body cardRenders always == 1 (only the mount time).
// If the implementation degenerates into subscribing to the whole Map: grid body cardRenders grows with frame count (== 1 + f), assertion 1 immediately red;
// if it degenerates into no memo/no granularity: card render increment would be == N × f (1000), assertion k×f immediately red.
//
// 【Why not use vi.useFakeTimers】
// Order 33 prohibited item 5. Not needed here either —— what's under test is the causality of "store commit → React commit count",
// independent of time. Just call applyTelemetryBatch directly to manufacture store-flush frames.

import { Profiler, type ProfilerOnRenderCallback, type ReactElement } from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import FleetOverviewCards from '../FleetOverviewCards';
import FleetHealthAnalytics from '../FleetHealthAnalytics';
import HealthSummaryHeader from '../HealthSummaryHeader';
import { buildRobotIds, offlinePlaceholder, useRobotStore } from '../../store/robotStore';
import { getSnapshot, resetObservability } from '../../lib/observability';
import * as tokens from '../../theme/tokens';
import type { RobotTelemetry } from '../../types/robot';

// jsdom has no canvas, rendering real ECharts throws (same handling as RobotDetailDrawer.test.tsx).
// The component body still executes normally —— we want to count the analytics-strip component's renders, not how many pixels ECharts drew.
vi.mock('echarts-for-react/lib/core', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

// Card-level render counter: RobotCard's function body calls robotDisplayState exactly once per render.
// Use importOriginal to keep the rest of the token exports (color/text assertions still use the real implementation).
vi.mock('../../theme/tokens', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../theme/tokens')>();
  return { ...actual, robotDisplayState: vi.fn(actual.robotDisplayState) };
});

const displayStateSpy = tokens.robotDisplayState as unknown as ReturnType<typeof vi.fn>;

/** Build a telemetry frame: given id list all set to "online + specified battery/temp", used to manufacture deterministic reference changes. */
function frame(ids: string[], seed: number): RobotTelemetry[] {
  const now = Math.floor(Date.now() / 1000);
  return ids.map((id, k) => ({
    ...offlinePlaceholder(id),
    status_code: 1,
    is_online: true,
    last_seen: now,
    battery_pct: 40 + ((seed + k) % 60),
    max_joint_temp: 35 + ((seed + k) % 45),
  })) as RobotTelemetry[];
}

/** Count subtree commits with Profiler (only for qualitative assertions like "did re-render"). */
function renderWithProfiler(onRender: ProfilerOnRenderCallback, ui: ReactElement) {
  return render(<Profiler id="guard" onRender={onRender}>{ui}</Profiler>);
}
function makeCommitCounter() {
  let n = 0;
  return { onRender: (() => { n += 1; }) as ProfilerOnRenderCallback, get count() { return n; } };
}

beforeEach(() => {
  resetObservability();
  displayStateSpy.mockClear();
});

afterEach(() => {
  displayStateSpy.mockClear();
});

describe('Order 33 T2 · card wall render guard (100 cards, changed count=k, frames=f)', () => {
  it('Baseline: exactly 100 cards rendered on mount (instrument self-proof — spy == card count)', () => {
    const { unmount } = render(<FleetOverviewCards />);
    // Each of the 100 cards calls robotDisplayState once. If one day cards stop rendering one-by-one (or the instrument fails), this goes red first.
    expect(displayStateSpy.mock.calls.length).toBe(100);
    unmount();
  });

  it('Card render increment == k × f (scales with changed count, not with N)', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    // 10 frames, only 3 change each frame
    const k = 3;
    const f = 10;
    const changed = buildRobotIds(100).slice(0, k);
    for (let i = 0; i < f; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(changed, i));
      });
    }

    // Ideal: only these 3 cards re-render, 3 × 10 = 30 times total.
    // Threshold uses **equality**: guards against both "render explosion" (>30) and "slacking off with fewer renders" (<30, meaning cards that should update didn't).
    expect(displayStateSpy.mock.calls.length - afterMount).toBe(k * f);
    unmount();
  });

  it('Reverse guard: k=100 full change render count == 100 × f (proves the previous test wasn\'t fooled by "fewer renders")', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    const all = buildRobotIds(100);
    const f = 5;
    for (let i = 0; i < f; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(all, i));
      });
    }

    // All 100 changed ⇒ all 100 cards must re-render (this is the **correct** cost, not what the guard blocks).
    // The point of this case: proves cards really do re-render on data change —— so the previous 3×10 can only come from
    // "only 3 changed", not from "the card wall doesn't re-render at all" kind of fake green.
    expect(displayStateSpy.mock.calls.length - afterMount).toBe(100 * f);
    unmount();
  });

  it('Idle frame (same reference pushed repeatedly): 0 card re-renders (structural sharing reference detection works)', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    // First give 3 cards real objects
    const changed = buildRobotIds(100).slice(0, 3);
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(frame(changed, 0));
    });
    const baseline = displayStateSpy.mock.calls.length;

    // Then push these 3 cards' **same batch of objects** repeatedly (simulating duplicate frames / server idle)
    const same = changed.map((id) => useRobotStore.getState().robots.get(id)!) as RobotTelemetry[];
    for (let i = 0; i < 10; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(same);
      });
    }

    // applyTelemetryBatch does per-entry reference-level detection: all same reference → returns original state → zero notification.
    expect(displayStateSpy.mock.calls.length).toBe(baseline);
    expect(displayStateSpy.mock.calls.length).toBeGreaterThan(afterMount); // the earlier real change still happened
    unmount();
  });

  it('Grid body itself (RobotGrid) doesn\'t re-render with store-flush frames: cardRenders always == 1', () => {
    render(<FleetOverviewCards />);
    // Mount is 1 (grid body renders once to build 100 cards)
    expect(getSnapshot().cardRenders).toBe(1);

    const all = buildRobotIds(100);
    for (let i = 0; i < 20; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(all, i));
      });
    }

    // After 20 frames of full flush, the grid body render count is **unchanged**: it only subscribes to selectedRobotId / selectRobot,
    // both unchanged during flush → zustand sends no notification. If someone changes it back to subscribing to the whole Map,
    // cardRenders would become 1 + 20 = 21, this assertion immediately red.
    expect(getSnapshot().cardRenders).toBe(1);
  });

  it('Grid body does re-render on selection-state change (proves the cardRenders instrument isn\'t a constant-1 decoration)', () => {
    render(<FleetOverviewCards />);
    const before = getSnapshot().cardRenders;

    act(() => {
      useRobotStore.getState().selectRobot('humanoid-042');
    });

    // The grid body subscribes to selectedRobotId, so selection-state change must re-render (to recompute 100 cards' selected).
    expect(getSnapshot().cardRenders).toBeGreaterThan(before);
  });
});

describe('Order 33 T2 · health analytics strip render guard (bucket-count selector + useShallow)', () => {
  it('No component re-render when bucket counts unchanged (data changes, buckets unchanged ⇒ shallow compare blocks)', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <FleetHealthAnalytics />);
    const afterMount = commits.count;

    // All 100 online, temp steady 30°C, battery steady 90 —— every frame's object reference changes, but
    // temp buckets stay [0,0,0,0,100], battery buckets stay [0,0,0,0,100], status buckets stay [100,0,0,0,0].
    const ids = buildRobotIds(100);
    for (let i = 0; i < 10; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(
          ids.map((id, k) => ({
            ...offlinePlaceholder(id),
            status_code: 1,
            is_online: true,
            last_seen: Math.floor(Date.now() / 1000),
            battery_pct: 90,
            max_joint_temp: 30 + ((i + k) % 3), // always < 40 → bucket unchanged
          })) as RobotTelemetry[],
        );
      });
    }

    // useShallow compares the three bucket results element-by-element: identical ⇒ no notification ⇒ component body commit count doesn't grow.
    // Threshold gives +1 margin: React may commit one extra under messages/side-effects, but it shouldn't grow with frame count.
    expect(commits.count).toBeLessThanOrEqual(afterMount + 1);
  });

  it('Must re-render when bucket counts change (prevents the previous test from being fake-green due to "component not subscribing")', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <FleetHealthAnalytics />);
    const afterMount = commits.count;

    const ids = buildRobotIds(100);
    // Frame 1: temp 30°C → temp buckets [100,0,0,0,0]
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(
        ids.map((id) => ({
          ...offlinePlaceholder(id), status_code: 1, is_online: true, battery_pct: 90, max_joint_temp: 30,
        })) as RobotTelemetry[],
      );
    });
    // Frame 2: temp 75°C → temp buckets flip to [0,0,0,0,100], bucket count truly changes
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(
        ids.map((id) => ({
          ...offlinePlaceholder(id), status_code: 1, is_online: true, battery_pct: 90, max_joint_temp: 75,
        })) as RobotTelemetry[],
      );
    });

    // Bucket count changed from [100,0,0,0,0] to [0,0,0,0,100] ⇒ must re-render, shallow compare can't eat the real change.
    expect(commits.count).toBeGreaterThan(afterMount);
  });
});

describe('Order 33 T2 · top-bar KPI render guard (derived stats + useShallow, float jitter doesn\'t penetrate)', () => {
  it('No top-bar re-render when derived stats (online rate/avg battery, 1 decimal) unchanged', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <HealthSummaryHeader />);
    const afterMount = commits.count;

    // All 100 online, battery steady 80 (avg 80.0), no alerts → top-bar display value constant.
    const ids = buildRobotIds(100);
    for (let i = 0; i < 10; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(
          ids.map((id) => ({
            ...offlinePlaceholder(id),
            status_code: 1,
            is_online: true,
            last_seen: Math.floor(Date.now() / 1000),
            battery_pct: 80,
            max_joint_temp: 40,
          })) as RobotTelemetry[],
        );
      });
    }

    // The selector rounds onlineRate / avgBattery to 1 decimal (consistent with toFixed(1) display precision),
    // so pure float jitter (like 80.00000001) won't penetrate the shallow compare. After 10 frames commit count shouldn't grow.
    expect(commits.count).toBeLessThanOrEqual(afterMount + 1);
  });
});
