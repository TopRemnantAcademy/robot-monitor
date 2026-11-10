// FleetOverviewCards (RobotGrid) component test (vitest + testing-library).
//
// Mirrors the canonical template src/components/__tests__/RobotDetailDrawer.test.tsx.
// RobotGrid reads the shared zustand store (robots Map / selectedRobotId / selectRobot / buildRobotIds(100))
// and renders the 100-robot card wall. We drive the store through its real API (NOT by mocking the store)
// using buildRobotIds + offlinePlaceholder + applyTelemetryBatch, then assert:
//   - 100 cards render and a known card (e.g. "H-042") is present;
//   - the online/offline split reflected in the store matches the seeded data;
//   - clicking a card calls selectRobot with that robot's id (drawer opens).

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import FleetOverviewCards from '../FleetOverviewCards';
import {
  buildRobotIds,
  offlinePlaceholder,
  selectStatusCounts,
  useRobotStore,
} from '../../store/robotStore';
import type { RobotTelemetry } from '../../types/robot';

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

/** Seed `n` robots as online with given battery/temp (rest stay offline placeholders). */
function seedOnline(n: number, battery = 80, temp = 40) {
  const now = Math.floor(Date.now() / 1000);
  const list = buildRobotIds(100)
    .slice(0, n)
    .map((id) => ({
      ...offlinePlaceholder(id),
      status_code: 1,
      is_online: true,
      last_seen: now,
      battery_pct: battery,
      max_joint_temp: temp,
    })) as RobotTelemetry[];
  useRobotStore.getState().applyTelemetryBatch(list);
}

beforeEach(() => {
  resetStore();
});

describe('FleetOverviewCards (RobotGrid)', () => {
  it('Renders 100 cards; a known card is present and status counts reflect seeded data', () => {
    seedOnline(10); // 10 online, 90 offline
    render(<FleetOverviewCards />);

    // 100 card buttons.
    expect(screen.getAllByRole('button')).toHaveLength(100);

    // A known card shows its short id "H-<nnn>".
    expect(screen.getByText('H-001')).toBeInTheDocument();
    expect(screen.getByText('H-042')).toBeInTheDocument();

    // Store-level counts match the seeded data (driven through the real store, not a mock).
    const counts = selectStatusCounts(useRobotStore.getState());
    expect(counts.total).toBe(100);
    expect(counts.online).toBe(10);
    expect(counts.offline).toBe(90);
  });

  it('Offline placeholder cards show "Offline" / "--" and 0% battery before any telemetry', () => {
    render(<FleetOverviewCards />); // all 100 still offline placeholders

    const card = screen.getByText('H-099').closest('button') as HTMLElement;
    expect(card).toBeInTheDocument();
    // Offline display state label (en) and no-temperature placeholder (nested °C span).
    expect(card.textContent).toContain('Offline');
    expect(card.textContent).toContain('--');
    expect(card.textContent).toContain('0%');
  });

  it('Clicking a card calls selectRobot with that robot id (opens the drawer)', () => {
    const selectRobotSpy = vi.spyOn(useRobotStore.getState(), 'selectRobot');

    render(<FleetOverviewCards />);

    const card = screen.getByText('H-042').closest('button') as HTMLElement;
    fireEvent.click(card);

    expect(selectRobotSpy).toHaveBeenCalledTimes(1);
    expect(selectRobotSpy).toHaveBeenCalledWith('humanoid-042');
    // The store's selectedRobotId is updated reflectively by the real action.
    expect(useRobotStore.getState().selectedRobotId).toBe('humanoid-042');
  });
});
