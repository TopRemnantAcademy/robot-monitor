// Order 24 T2 — RobotDetailDrawer component test (vitest + testing-library).
//
// Why pick the Drawer over other components:
//   STATUS.md §six #2 recorded a **real incident** —— `/history` didn't return
//   `joint_positions`, causing the Drawer's third line chart "Angle (rad)" to be permanently empty, spotted by eye.
//   This case nails the chain "REST returns → component parses → hands series to ECharts" into an assertion,
//   so renaming a field goes red here instead of waiting until a line is missing on the dashboard.
//
// About ECharts: jsdom has no canvas, rendering real ECharts throws. Here we mock
// `echarts-for-react/lib/core`, dumping the `option` it receives into the DOM (data-testid),
// so we can assert the "chart model computed by the component" layer —— which is exactly the pure-logic layer that should be tested,
// while how ECharts itself draws pixels is outside this repo's responsibility.
//
// NOTE — Network layer: why use a controlled fetch double instead of msw (Issue #78 root cause):
//   happy-dom overrides the global `Response`/`ReadableStream` with its own implementation, while msw/node
//   interceptor-returned Response bodies are Node (undici) ReadableStreams —— happy-dom's
//   `Response.json()` can't read this stream, so in the 200 case `res.json()` hangs forever (only reading
//   `res.status` in the 500 case works normally). This is an **environment-layer** problem, unrelated to the component implementation.
//   Measured: msw doesn't work in this sandbox, so here we use a controlled global `fetch` double: it returns the native Node
//   `Response` (its `.json()` parses correctly), preserving the "real fetch → parse → chart" chain assertion,
//   while bypassing the environment hang. The component still uses the real `fetchRobotHistory` (via global fetch), so
//   regressions like wrong URL / missing header / renamed field are still caught by this case.

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import RobotDetailDrawer from '../RobotDetailDrawer';
import { useRobotStore } from '../../store/robotStore';
import { useAuthStore } from '../../auth/authStore';
import { JOINT_NAMES } from '../../types/robot';
import type { RobotTelemetry } from '../../types/robot';
import { SAMPLE_HISTORY } from '../../test/msw/handlers';
// Shared render: already wrapped in QueryClientProvider, so components using useQuery can render in tests
// (fixes Issue #78: main previously had npm test 8/8 all red).
import { render } from '../../test/render';

/** Drawer "this robot's recent alerts" case's /alerts response: one local + one other-robot (verifies filtering). */
const SAMPLE_ALERT_PAGE = {
  items: [
    {
      alert_id: 11,
      time: '2026-10-10T23:48:00Z',
      robot_id: 'humanoid-042',
      rule_id: 'motor_temp',
      severity: 3,
      message: 'Left elbow motor over temp',
      detail: null,
      ack: false,
      ack_at: null,
      ack_by: null,
      escalated: true,
    },
    {
      alert_id: 12,
      time: '2026-10-10T23:40:00Z',
      robot_id: 'humanoid-007',
      rule_id: 'battery_low',
      severity: 2,
      message: 'Low battery',
      detail: null,
      ack: true,
      ack_at: null,
      ack_by: null,
      escalated: false,
    },
  ],
  total: 2,
  limit: 200,
  offset: 0,
};

// —— ECharts double: serialize option into DOM for assertions ——
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}));

// —— Controlled fetch double (replaces msw, bypassing happy-dom + msw body hang) ——
// Default implementation: hit /history returns sample data (SAMPLE_HISTORY), everything else 200 empty JSON.
// Each case can use `fetchMock.mockImplementation(...)` to temporarily rewrite the response (see the 500 / empty / auth cases below).
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// The signature deliberately **has a second parameter** (`RequestInit`), though the default implementation doesn't use it:
// the downstream `Authorization` case needs `init.headers` to assert the request header; if only 1 parameter is declared here,
// `mockImplementation(async (_input, init) => ...)`'s second arg degrades to implicit any
// (TS7006), and the overall signature is incompatible (TS2345) —— this is the sole source of the 3 long-standing errors in `npm run build`'s
// `tsc --noEmit` (ISSUE #62 acceptance requires 0 errors).
// Leading underscore on the param name is the convention for "I really don't use it", not a typo.
const fetchMock = vi.fn(
  async (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => jsonResponse(SAMPLE_HISTORY),
);

beforeAll(() => {
  vi.stubGlobal('fetch', fetchMock);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

function telemetry(over: Partial<RobotTelemetry> = {}): RobotTelemetry {
  const zeros = Object.fromEntries(JOINT_NAMES.map((j) => [j, 0]));
  return {
    robot_id: 'humanoid-042',
    status_code: 2,
    battery_pct: 18.4,
    max_joint_temp: 66.2,
    joint_temperatures: { ...zeros, left_elbow_pitch: 62.5 },
    joint_torques: { ...zeros, left_elbow_pitch: 1.7 },
    joint_positions: { ...zeros, left_elbow_pitch: 0.55 },
    error_code: 0,
    ts: Date.parse('2026-10-10T23:49:10Z'),
    // ISSUE #77: is_online is now a required field in the generation contract.
    // Taking `true` is deliberate: every case in this file verifies the rendering of "a robot with data",
    // taking false would make the Drawer show "lost contact" and hide the temperature, changing output unrelated to the assertion intent.
    // The real offline/lost-contact rendering assertions are in src/store/__tests__/robotStore.liveness.test.ts.
    is_online: true,
    last_seen: Math.floor(Date.parse('2026-10-10T23:49:10Z') / 1000),
    ...over,
  };
}

/** Preset a robot into the store and open the Drawer. */
function openDrawer(id = 'humanoid-042') {
  const st = useRobotStore.getState();
  st.applyTelemetryBatch([telemetry({ robot_id: id })]);
  st.selectRobot(id);
}

interface ChartOption {
  series: { name: string; data: (number | null)[] }[];
}

/**
 * The Drawer now mounts two charts (joint line + battery trend), the mock renders the same testid <pre> for each instance.
 * index 0 = joint line (first in DOM order), index 1 = battery trend.
 */
async function readChartOption(index = 0): Promise<ChartOption> {
  const els = await screen.findAllByTestId('echarts-option');
  return JSON.parse(els[index]?.textContent ?? '{}') as ChartOption;
}

describe('RobotDetailDrawer', () => {
  it('No content rendered when no robot selected (avoid empty Drawer covering main area)', () => {
    const { container } = render(<RobotDetailDrawer />);
    expect(container).toBeEmptyDOMElement();
  });

  it('Render selected robot basic info: battery / max joint temp / status label', () => {
    openDrawer();
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    expect(within(aside).getByText('humanoid-042')).toBeInTheDocument();
    expect(within(aside).getByText('18.4%')).toBeInTheDocument();      // battery_pct
    expect(within(aside).getByText('66.2°C')).toBeInTheDocument();     // max_joint_temp
    // status_code=2 → Warning (see theme/tokens DISPLAY_COLOR mapping, i18n uses display.warning)
    expect(within(aside).getByText('Warning')).toBeInTheDocument();
  });

  it('After fetching history, split 24 joints and envelope into four series for ECharts', async () => {
    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(async () => {
      const parsed = await readChartOption();
      expect(parsed.series.map((s) => s.name)).toEqual([
        'Temp (°C)', 'Torque (N·m)', 'Angle (rad)', 'Max joint temp (°C)',
      ]);
      // Default-selected joint is the 0th (JOINT_NAMES[0]); sample data has no value for it → null
      expect(parsed.series[0].data).toEqual([null, null]);
      // Envelope line takes the backend's per-bucket aggregated max_joint_temp (66.2 / 67.8), independent of selected joint
      expect(parsed.series[3].data).toEqual([66.2, 67.8]);
    });
  });

  it('After switching selected joint, take that joint\'s joint_positions (regression: angle line was once always empty)', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    // Heatmap joint block's aria-label is prefixed with the full joint name (e.g. "left_elbow_pitch 62.5°C")
    const chip = await screen.findByRole('button', { name: /^left_elbow_pitch/ });
    await user.click(chip);

    await waitFor(async () => {
      const parsed = await readChartOption();
      // left_elbow_pitch in the two sample points are 60.0/62.5 (temp), 1.5/1.7 (torque), 0.42/0.55 (position)
      expect(parsed.series[0].data).toEqual([60, 62.5]);
      expect(parsed.series[1].data).toEqual([1.5, 1.7]);
      expect(parsed.series[2].data).toEqual([0.42, 0.55]);
      // Switching joint doesn't affect the envelope line (max_joint_temp is independent of selected joint)
      expect(parsed.series[3].data).toEqual([66.2, 67.8]);
    });
  });

  it('Show "no history points" instead of empty chart when history is empty', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({ robot_id: 'humanoid-042', points: [] }),
    );
    openDrawer();
    render(<RobotDetailDrawer />);

    expect(await screen.findByText(/No history points/)).toBeInTheDocument();
    // Joint chart and battery trend chart share the same history — empty means neither should mount
    expect(screen.queryAllByTestId('echarts-option')).toHaveLength(0);
  });

  it('Show error state on history 500, and error text contains HTTP status code (for troubleshooting)', async () => {
    fetchMock.mockImplementation(async () => new Response(null, { status: 500 }));
    openDrawer();
    render(<RobotDetailDrawer />);

    // 5xx goes through queryClient's retry (lib/queryClient.ts:27: no retry on 4xx, retry others up to 2 times),
    // retryDelay = min(1000 * 2^attempt, 8000) → 1s + 2s ≈ 3s before landing on error state.
    // Default findByText only has 1s, would judge failure mid-retry. Here explicitly relax to 6s (enough margin),
    // assertion content unchanged —— still requires the error text to carry the HTTP status code.
    expect(
      await screen.findByText(/History unavailable/, undefined, { timeout: 6000 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
  }, 10_000);

  it('Close button clears selectedRobotId (Drawer unmounts)', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    await user.click(screen.getByRole('button', { name: 'Close' }));
    expect(useRobotStore.getState().selectedRobotId).toBeNull();
  });

  it('Request must carry Authorization header (Order 15: the only REST call point, miss it and everything leaks)', async () => {
    const seen: (string | null)[] = [];
    fetchMock.mockImplementation(async (_input, init) => {
      seen.push(new Headers(init?.headers).get('authorization'));
      return jsonResponse(SAMPLE_HISTORY);
    });
    // Preset token (sessionStorage)
    window.sessionStorage.setItem('robotmonitor.jwt', 'test-token');

    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen[0]).toBe('Bearer test-token');
  });

  it('Alert area filters by robot_id: only this robot\'s alerts shown, escalation marker visible', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/alerts')) return jsonResponse(SAMPLE_ALERT_PAGE);
      return jsonResponse(SAMPLE_HISTORY);
    });
    openDrawer();
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    // humanoid-042's motor_temp alert renders (ruleLabel reuses AlertLogTable's mapping)
    expect(await within(aside).findByText('Motor overheat')).toBeInTheDocument();
    expect(within(aside).getByText('Escalated')).toBeInTheDocument();
    // humanoid-007's low-battery alert must not bleed through (both rule label and message appearance points verified)
    expect(within(aside).queryByText('Low battery')).not.toBeInTheDocument();
    expect(within(aside).queryByText('Low battery')).not.toBeInTheDocument();
  });

  // —— Order 27 added three cases —— (doesn't change any assertion of the existing 9 cases)

  it('Torque load bar: aria-label carries joint name and real-time value, click switches selected joint (linked with heatmap)', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    // aria-label deliberately starts with "Torque ": the heatmap joint block's aria-label starts with the joint name
    // (/^left_elbow_pitch/ case), the two must not be confused on the accessibility tree.
    // Real-time frame telemetry().joint_torques.left_elbow_pitch = 1.7 → toFixed(2) = "1.70"
    const bar = await screen.findByRole('button', { name: 'Torque left_elbow_pitch 1.70N·m' });
    await user.click(bar);

    // After click, selected joint switches → the line chart's torque series takes left_elbow_pitch's two history points
    await waitFor(async () => {
      const parsed = await readChartOption();
      expect(parsed.series[1].data).toEqual([1.5, 1.7]);
    });
  });

  it('History window switch: click 30m then request minutes=30, title changes with window', async () => {
    const user = userEvent.setup();
    const urls: string[] = [];
    fetchMock.mockImplementation(async (input) => {
      urls.push(String(input));
      return jsonResponse(SAMPLE_HISTORY);
    });
    openDrawer();
    render(<RobotDetailDrawer />);

    await user.click(await screen.findByRole('button', { name: '30m' }));
    // queryKey contains minutes → switching window necessarily produces a new minutes=30 request
    await waitFor(() => expect(urls.some((u) => u.includes('minutes=30'))).toBe(true));
    // Other params in the request don't regress (resolution always 10s)
    expect(urls.some((u) => u.includes('minutes=30') && u.includes('resolution=10s'))).toBe(true);
    // Both the joint chart header and battery trend header must reflect the new window (exact full-string match, avoid substring hitting multiple elements)
    expect(screen.getByText('left_shoulder_pitch · Last 30 min (10s resolution)')).toBeInTheDocument();
    expect(screen.getByText('Battery trend · Last 30 min')).toBeInTheDocument();
  });

  // Placed at the end of describe: this case promotes authStore to admin, restores before finishing, to avoid polluting other cases.
  it('Alert acknowledge: no button for non-admin; admin click-acknowledge then refetch reflects ack state', async () => {
    const user = userEvent.setup();
    let acked = false;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/alerts')) {
        // POST {API_BASE}/alerts/{id}/ack —— AckResult contract (types/robot.ts)
        if (init?.method === 'POST') {
          acked = true;
          return jsonResponse({ alert_id: 11, ack: true, ack_at: '2026-10-10T23:50:00Z', ack_by: 'a@b.c', changed: true });
        }
        // GET /alerts: the first return after ack flips alert 11 to ack=true (authoritative view reconciliation)
        const page = {
          ...SAMPLE_ALERT_PAGE,
          items: [{ ...SAMPLE_ALERT_PAGE.items[0], ack: acked }, SAMPLE_ALERT_PAGE.items[1]],
        };
        return jsonResponse(page);
      }
      return jsonResponse(SAMPLE_HISTORY);
    });
    openDrawer();
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    // Non-admin (default role=null): acknowledge button not rendered —— "don't let users see the unclickable thing"
    await within(aside).findByText('Motor overheat');
    expect(within(aside).queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();

    // Promote to admin → button appears → click → POST /alerts/11/ack → invalidate refetch → acknowledged
    useAuthStore.setState({ status: 'authenticated', email: 'a@b.c', role: 'admin' });
    await user.click(await within(aside).findByRole('button', { name: 'Acknowledge' }));
    await waitFor(() => expect(acked).toBe(true));
    expect(await within(aside).findByText('Acknowledged')).toBeInTheDocument();

    // Restore global auth state (why this case must stay at the end of describe)
    useAuthStore.setState({ status: 'unknown', email: null, role: null });
  });
});
