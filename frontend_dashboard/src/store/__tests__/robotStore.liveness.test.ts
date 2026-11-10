// ISSUE #77 regression: online judgment must come from the backend's is_online, not status_code.
//
// WHY A SEPARATE FILE INSTEAD OF MERGING INTO THE COMPONENT TEST
// This group asserts the **pure logic** of the store/selectors: given a robots Map, what is the online count.
// It doesn't render components, doesn't need QueryClientProvider, doesn't depend on msw —— so it can independently
// verify this issue's core conclusion, separate from Issue #78 (Drawer test base missing a provider).
// Verified fact: this file is all green while `RobotDetailDrawer.test.tsx` is still 8/8 red —— the two don't interfere.

import { describe, expect, it } from 'vitest';
import { offlinePlaceholder, selectStatusCounts, useRobotStore } from '../robotStore';
import { isRobotOnline } from '../../types/robot';
import type { RobotStatus, RobotTelemetry } from '../../types/robot';

/** Build a telemetry: status_code is the robot's **self-reported** last frame, is_online is the **backend's** judgment. */
function telemetry(
  robotId: string,
  statusCode: RobotStatus,
  isOnline: boolean,
  lastSeen: number | null,
): RobotTelemetry {
  return {
    ...offlinePlaceholder(robotId),
    status_code: statusCode,
    is_online: isOnline,
    last_seen: lastSeen,
    battery_pct: 80,
    max_joint_temp: 45,
  };
}

describe('ISSUE #77 · selectStatusCounts only trusts is_online', () => {
  it('A robot that stopped reporting but whose last frame was "normal" must count as offline (the old impl counted it online)', () => {
    const now = Math.floor(Date.now() / 1000);
    const robots = new Map<string, RobotTelemetry>([
      // Seen an hour ago, then stopped; its self-reported status_code is stuck at 1 (normal)
      ['humanoid-001', telemetry('humanoid-001', 1, false, now - 3600)],
      // Currently reporting
      ['humanoid-002', telemetry('humanoid-002', 3, true, now)],
    ]);

    const counts = selectStatusCounts({ robots });
    expect(counts.total).toBe(2);
    expect(counts.online).toBe(1);
    expect(counts.offline).toBe(1);
    // "Lost" = received before, now stopped —— counted separately from "never reported" (different handling action)
    expect(counts.stale).toBe(1);
  });

  it('A never-reported placeholder card is offline and not "lost" (first-paint semantics must not break)', () => {
    const robots = new Map<string, RobotTelemetry>([
      ['humanoid-001', offlinePlaceholder('humanoid-001')],
    ]);
    const counts = selectStatusCounts({ robots });
    expect(counts.online).toBe(0);
    expect(counts.offline).toBe(1);
    expect(counts.stale).toBe(0);
    // The placeholder must explicitly declare is_online=false, rather than letting the downstream guess from status_code===0
    expect(offlinePlaceholder('humanoid-002').is_online).toBe(false);
    expect(offlinePlaceholder('humanoid-002').last_seen).toBeNull();
  });

  it('Reverse guardrail: the old criterion (status_code !== 0) yields a different conclusion in this scenario', () => {
    const stale = telemetry('humanoid-001', 1, false, 1_600_000_000);
    const newVerdict = isRobotOnline(stale);
    const oldVerdict = stale.status_code !== 0;
    expect(newVerdict).toBe(false);
    expect(oldVerdict).toBe(true);
    // The two are not equivalent ⇒ the assertions above are truly watching "which field is the basis"
    expect(newVerdict).not.toBe(oldVerdict);
  });

  it('When a field is missing (legacy backend), conservatively judge offline, never falsely report online', () => {
    const legacy = { ...telemetry('humanoid-001', 1, false, null) } as Partial<RobotTelemetry>;
    delete legacy.is_online;
    expect(isRobotOnline(legacy as RobotTelemetry)).toBe(false);
  });
});

describe('ISSUE #77 · applySnapshot does not drop the backend-issued online flag', () => {
  it('is_online / last_seen are preserved after the snapshot is persisted', () => {
    const now = Math.floor(Date.now() / 1000);
    useRobotStore.getState().applySnapshot([telemetry('humanoid-007', 2, true, now)]);
    const got = useRobotStore.getState().robots.get('humanoid-007');
    expect(got?.is_online).toBe(true);
    expect(got?.last_seen).toBe(now);
    expect(got?.status_code).toBe(2);

    // Robots absent from the snapshot fall back to offline placeholder (the first-paint 100-card skeleton must remain)
    const missing = useRobotStore.getState().robots.get('humanoid-008');
    expect(missing?.is_online).toBe(false);
  });
});
