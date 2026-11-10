// Order 35 T3 — alert "suppression" presentation component test (vitest + testing-library).
//
// Pins down three things (each is a point where "writing it wrong causes regression"):
//   1. Suppressed rows **must be present** with a "Suppressed" marker + readable root-cause-rule title;
//      "reduce visual weight rather than delete" is a traceability requirement, a disappearing row is treated as regression.
//   2. The "Show suppressed" toggle is **off** by default: the main list request must explicitly carry `suppressed=false`
//      (suppressed items don't enter the unacknowledged stream); after turning on, a separate `suppressed=true` request is sent.
//   3. `RobotDetailDrawer`'s local-alert area also marks —— and it does **not** filter suppressed
//      (when troubleshooting you need to see "why didn't this pop up").
//
// NOTE — Network layer: controlled fetch double, not msw (Issue #78 root cause):
//   happy-dom overrides the global `Response`/`ReadableStream` with its own implementation, while msw/node
//   interceptor-returned bodies are Node (undici) ReadableStreams —— happy-dom's
//   `Response.json()` can't read this stream, the 200 case hangs forever (only the 500 case that reads res.status
//   works normally). This repo's vitest has switched back to jsdom (vitest.config.ts comments have the full measured record),
//   but to keep the same writing style as the existing `RobotDetailDrawer.test.tsx` (controlled double + assert real URL),
//   here we continue with the controlled double: it returns the native Node `Response`, both bypassing environment differences,
//   and catching regressions like "wrong URL / missing filter param / renamed field".

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AlertLogTable, { ALERT_PAGE_SIZE } from '../AlertLogTable';
import RobotDetailDrawer from '../RobotDetailDrawer';
import { useRobotStore } from '../../store/robotStore';
import { JOINT_NAMES } from '../../types/robot';
import type { RobotTelemetry } from '../../types/robot';
import { SAMPLE_HISTORY } from '../../test/msw/handlers';
import { render } from '../../test/render';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * A **suppressed** alert: humanoid-042's battery_low (sev2),
 * pinned down by the same machine's motor_temp (sev3) —— same shape as alert_engine's InhibitionRecord.
 */
const SUPPRESSED_ITEM = {
  alert_id: 21,
  time: '2026-10-10T23:45:00Z',
  robot_id: 'humanoid-042',
  rule_id: 'battery_low',
  severity: 2,
  message: 'Battery 17.4% below 20%',
  detail: { suppressed_reason: 'severity', suppressed_source_rule: 'motor_temp' },
  ack: false,
  ack_at: null,
  ack_by: null,
  escalated: false,
  suppressed: true,
  suppressed_by: 'motor_temp',
};

/** A normal unacknowledged alert (control group: must **not** be marked). */
const NORMAL_ITEM = {
  alert_id: 22,
  time: '2026-10-10T23:44:00Z',
  robot_id: 'humanoid-042',
  rule_id: 'joint_stuck',
  severity: 2,
  message: 'Left shoulder joint stuck',
  detail: null,
  ack: false,
  ack_at: null,
  ack_by: null,
  escalated: false,
  suppressed: false,
  suppressed_by: null,
};

/** Records the full URL of each /alerts request, for the "filter param" assertion. */
const alertsUrls: string[] = [];

/**
 * Controlled fetch: routes by the `suppressed` param in the URL.
 * `suppressed=true` → only the suppressed one; otherwise → only the normal one (simulating the backend's tri-state filter).
 * This way "sends true when switch is off / sends false when switch is on" are both immediately caught by assertions.
 */
function urlBehaviour(input: RequestInfo | URL): Response {
  const url = String(input);
  if (!url.includes('/alerts')) return jsonResponse(SAMPLE_HISTORY);
  alertsUrls.push(url);
  const wantSuppressed = new URL(url, 'http://localhost').searchParams.get('suppressed');
  const items = wantSuppressed === 'true' ? [SUPPRESSED_ITEM] : [NORMAL_ITEM];
  return jsonResponse({ items, total: items.length, limit: ALERT_PAGE_SIZE, offset: 0 });
}

// —— ECharts double (same writing style as RobotDetailDrawer.test.tsx) ——
// jsdom has no canvas, rendering real ECharts throws `Cannot read properties of null (reading
// 'clearRect')` (measured on this machine, zrender's Layer.clear). Just dump option into DOM:
// this file tests the suppression marker, how the chart draws pixels is out of scope.
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}));

const fetchMock = vi.fn(
  async (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
    jsonResponse(SAMPLE_HISTORY),
);

beforeAll(() => {
  vi.stubGlobal('fetch', fetchMock);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

function telemetry(id = 'humanoid-042'): RobotTelemetry {
  const zeros = Object.fromEntries(JOINT_NAMES.map((j) => [j, 0]));
  return {
    robot_id: id,
    status_code: 2,
    battery_pct: 18.4,
    max_joint_temp: 66.2,
    joint_temperatures: { ...zeros, left_elbow_pitch: 62.5 },
    joint_torques: { ...zeros, left_elbow_pitch: 1.7 },
    joint_positions: { ...zeros, left_elbow_pitch: 0.55 },
    error_code: 0,
    ts: Date.parse('2026-10-10T23:49:10Z'),
    is_online: true,
    last_seen: Math.floor(Date.parse('2026-10-10T23:49:10Z') / 1000),
  };
}

/**
 * Reset the double and request records at the start of each case (cases don't share call history).
 *
 * **Deliberately not using `mockReset()`**: reset clears the implementation to undefined, while after `render()`
 * react-query's first query might still be in-flight between this `arm()` and the next `arm()` ——
 * reset causes a fake failure of "first request returns undefined.json crash" (measured on this machine).
 * Only swapping the implementation achieves the "per-case isolation" goal.
 */
function arm(impl = urlBehaviour): void {
  alertsUrls.length = 0;
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => impl(input));
}

describe('AlertLogTable — suppression presentation (Order 35)', () => {
  it('Toggle off by default: main list request carries suppressed=false, and suppressed row not rendered', async () => {
    arm();
    render(<AlertLogTable />);

    // Key: main list **explicitly** carries suppressed=false (doesn't rely on backend default, contract deliberately neutral)
    await waitFor(() => expect(alertsUrls.length).toBeGreaterThan(0));
    expect(alertsUrls[0]).toContain('suppressed=false');
    // Normal row present (ruleLabel: joint_stuck → Joint stuck).
    // Use waitFor not findByText: react-query's resolve → setState → re-render
    // is an async chain, findByText's 1s default window occasionally times out on this machine.
    await waitFor(() => expect(screen.getByText('Left shoulder joint stuck')).toBeInTheDocument());
    expect(screen.getByText('Joint stuck · S2')).toBeInTheDocument();
    // Suppressed row not in default view (backend already filtered by suppressed=false, frontend shouldn't show it either)
    expect(screen.queryByText('⊘ Suppressed')).not.toBeInTheDocument();
    // Switch is off
    expect(screen.getByRole('switch', { name: /Show suppressed/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('After opening "Show suppressed" fetch suppressed=true separately, and mark "Suppressed" inline + root-cause title', async () => {
    const user = userEvent.setup();
    arm();
    render(<AlertLogTable />);

    await user.click(await screen.findByRole('switch', { name: /Show suppressed/ }));

    // Dedicated query carries suppressed=true (a separate request from the main list's false)
    await waitFor(() => expect(alertsUrls.some((u) => u.includes('suppressed=true'))).toBe(true));

    // Row **present** (reduce visual weight rather than delete) and marker visible
    const tag = await screen.findByText('⊘ Suppressed');
    expect(tag).toBeInTheDocument();
    // Readable attribution: title translates the root-cause rule to an English label (motor_temp → Motor overheat)
    expect(tag).toHaveAttribute('title', 'Suppressed by higher-severity alert (root cause: Motor overheat)');
    // The suppressed row is still in the table (same AlertRow render path), message text queryable
    expect(screen.getByText('Battery 17.4% below 20%')).toBeInTheDocument();
    // Turn off switch back to default: marker disappears (but cache not cleared, so no refetch)
    await user.click(screen.getByRole('switch', { name: /Show suppressed/ }));
    await waitFor(() => expect(screen.queryByText('⊘ Suppressed')).not.toBeInTheDocument());
  });

  it('Suppressed row only reduces visual weight: opacity/grayscale take effect but row not removed (traceable)', async () => {
    const user = userEvent.setup();
    arm();
    const { container } = render(<AlertLogTable />);

    await user.click(await screen.findByRole('switch', { name: /Show suppressed/ }));
    const tag = await screen.findByText('⊘ Suppressed');

    // The row container of the marker's button = the row's root div (AlertRow's <div> wraps a <button>)
    const rowDiv = tag.closest('button')?.parentElement as HTMLElement | null;
    expect(rowDiv).not.toBeNull();
    // Reduce weight: opacity < 1 and grayscale; **not** display:none (row must still be in DOM)
    expect(rowDiv!.style.opacity).toBe('0.45');
    expect(rowDiv!.style.filter).toBe('grayscale(1)');
    expect(rowDiv!.style.display).not.toBe('none');
    expect(container.contains(rowDiv)).toBe(true);
  });
});

describe('RobotDetailDrawer — suppression marker (Order 35)', () => {
  it('Local-alert area marks "Suppressed", and does **not** filter suppressed (troubleshooting needs to see the suppressed rows)', async () => {
    arm((input) => {
      const url = String(input);
      if (url.includes('/alerts')) {
        // Drawer deliberately doesn't carry suppressed param: wants the full set, to answer "why didn't this pop up"
        return jsonResponse({ items: [SUPPRESSED_ITEM, NORMAL_ITEM], total: 2, limit: 200, offset: 0 });
      }
      return jsonResponse(SAMPLE_HISTORY);
    });

    const st = useRobotStore.getState();
    st.applyTelemetryBatch([telemetry()]);
    st.selectRobot('humanoid-042');
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('dialog');
    // Suppressed row present + marker readable (reuses AlertLogTable's same wording and token)
    const tag = await within(aside).findByText('⊘ Suppressed');
    expect(tag).toHaveAttribute('title', 'Suppressed by higher-severity alert (root cause: Motor overheat)');
    expect(within(aside).getByText('Battery 17.4% below 20%')).toBeInTheDocument();
    // Control group: normal alert renders but **without** marker —— marker belongs only to the suppressed one
    expect(within(aside).getByText('Left shoulder joint stuck')).toBeInTheDocument();
    expect(within(aside).getAllByText('⊘ Suppressed')).toHaveLength(1);

    // The Drawer's request has **no** suppressed filter param (deliberately wants the full set: suppressed rows are the focus when troubleshooting).
    // This case uses a **custom** implementation (routes by endpoint), no longer going through `arm()`'s default
    // `urlBehaviour` —— so this assertion doesn't use `alertsUrls` (that's the recorder in the default implementation),
    // but takes the actual URL from the double's call records. The Drawer sends /history first then /alerts,
    // order not decided by the test, so scan all calls to take that one /alerts.
    const calls = fetchMock.mock.calls.map((c) => String(c[0]));
    const alertCall = calls.find((u) => u.includes('/alerts'));
    expect(alertCall, `No /alerts request was issued; actual: ${JSON.stringify(calls)}`).toBeDefined();
    expect(alertCall).not.toContain('suppressed=');
  });
});
