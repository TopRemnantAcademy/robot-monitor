// 订单 24 T2 — RobotDetailDrawer 组件测试（vitest + testing-library）。
//
// 为什么挑 Drawer 而不是别的组件：
//   STATUS.md §六第 2 条记录过一次**真实事故** —— `/history` 不返回
//   `joint_positions`，导致 Drawer 第三条折线「角度(rad)」恒空，靠人眼看出来的。
//   本用例把「REST 返回 → 组件解析 → 交给 ECharts 的 series」这条链钉成断言，
//   再改字段名就会在这里红，而不是等到大屏上少一条线才被发现。
//
// 关于 ECharts：jsdom 没有 canvas，真渲染 ECharts 会抛错。这里 mock 掉
// `echarts-for-react/lib/core`，把它收到的 `option` 原样 dump 到 DOM（data-testid），
// 于是我们能断言「组件算出来的图表模型」这一层 —— 这正是最该测的纯逻辑层，
// 而 ECharts 自身怎么画像素不在本仓职责内。
//
// 【网络层：为什么用受控 fetch 替身而不是 msw（Issue #78 根因）】
//   happy-dom 会把全局 `Response`/`ReadableStream` 覆盖成自己的实现，而 msw/node
//   拦截器返回的 Response body 是 Node(undici) 的 ReadableStream —— happy-dom 的
//   `Response.json()` 读不动这条流，于是在 200 用例里 `res.json()` 永久挂起（只读
//   `res.status` 的 500 用例反而正常）。这是**环境层**问题，与组件实现无关。
//   实测本沙箱内 msw 这条路不通，故这里改用受控的全局 `fetch` 替身：它返回 Node 原生
//   `Response`（其 `.json()` 可正确解析），既保留「真实 fetch → 解析 → chart」链路断言，
//   又绕开环境挂起。组件里仍走真实的 `fetchRobotHistory`（用全局 fetch），所以
//   URL 拼错 / 头漏带 / 字段改名 这一类回归依然会被本用例抓住。

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import RobotDetailDrawer from '../RobotDetailDrawer';
import { useRobotStore } from '../../store/robotStore';
import { JOINT_NAMES } from '../../types/robot';
import type { RobotTelemetry } from '../../types/robot';
import { SAMPLE_HISTORY } from '../../test/msw/handlers';
// 共享渲染：已包裹 QueryClientProvider，使使用 useQuery 的组件可在测试里渲染
// （修复 Issue #78：main 此前 npm test 8/8 全红）。
import { render } from '../../test/render';

/** 抽屉「该机器人最近告警」用例的 /alerts 响应：一条本机 + 一台他机（验证过滤）。 */
const SAMPLE_ALERT_PAGE = {
  items: [
    {
      alert_id: 11,
      time: '2026-10-10T23:48:00Z',
      robot_id: 'humanoid-042',
      rule_id: 'motor_temp',
      severity: 3,
      message: '左肘电机温度超限',
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
      message: '电量低',
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

// —— ECharts 替身：把 option 序列化进 DOM，供断言 ——
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}));

// —— 受控 fetch 替身（替代 msw，绕开 happy-dom + msw 的 body 挂起）——
// 默认实现：命中 /history 就返回样例数据（SAMPLE_HISTORY），其余一律 200 空 JSON。
// 各用例可用 `fetchMock.mockImplementation(...)` 临时改写响应（见下方面 500 / 空 / 鉴权用例）。
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// 签名刻意**带第二个形参**（`RequestInit`），尽管默认实现用不到它：
// 下游 `Authorization` 用例要用 `init.headers` 断言请求头，若这里只声明 1 个参数，
// `mockImplementation(async (_input, init) => ...)` 的第二参就退化成隐式 any
// （TS7006），并且整体签名不兼容（TS2345）—— 这是 `npm run build` 里
// `tsc --noEmit` 长期存在 3 个错误的唯一来源（ISSUE #62 验收要求 0 错误）。
// 形参名前导下划线是"我真的不用它"的约定，不是笔误。
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
    // ISSUE #77：is_online 现在是生成契约里的 required 字段。
    // 取 `true` 是刻意的：本文件的用例全部在验「有数据的机器人」的渲染，
    // 取 false 会让 Drawer 显示「失联」并隐藏温度，与断言意图无关地改变输出。
    // 真正的离线/失联渲染断言在 src/store/__tests__/robotStore.liveness.test.ts。
    is_online: true,
    last_seen: Math.floor(Date.parse('2026-10-10T23:49:10Z') / 1000),
    ...over,
  };
}

/** 预置一台机器人到 store 并打开 Drawer。 */
function openDrawer(id = 'humanoid-042') {
  const st = useRobotStore.getState();
  st.applyTelemetryBatch([telemetry({ robot_id: id })]);
  st.selectRobot(id);
}

interface ChartOption {
  series: { name: string; data: (number | null)[] }[];
}

/**
 * Drawer 现在挂两块图（关节折线 + 电量趋势），mock 对每个实例都渲染同 testid 的 <pre>。
 * index 0 = 关节折线（DOM 序在前），index 1 = 电量趋势。
 */
async function readChartOption(index = 0): Promise<ChartOption> {
  const els = await screen.findAllByTestId('echarts-option');
  return JSON.parse(els[index]?.textContent ?? '{}') as ChartOption;
}

describe('RobotDetailDrawer', () => {
  it('未选中机器人时不渲染任何内容（避免空 Drawer 遮住主区）', () => {
    const { container } = render(<RobotDetailDrawer />);
    expect(container).toBeEmptyDOMElement();
  });

  it('渲染选中机器人的基本信息：电量 / 最高关节温度 / 状态标签', () => {
    openDrawer();
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    expect(within(aside).getByText('humanoid-042')).toBeInTheDocument();
    expect(within(aside).getByText('18.4%')).toBeInTheDocument();      // battery_pct
    expect(within(aside).getByText('66.2°C')).toBeInTheDocument();     // max_joint_temp
    // status_code=2 → 预警（见 theme/tokens 的 STATUS_LABEL）
    expect(within(aside).getByText('预警')).toBeInTheDocument();
  });

  it('拉取 history 后把 24 关节与包络拆成 四条 series 交给 ECharts', async () => {
    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(async () => {
      const parsed = await readChartOption();
      expect(parsed.series.map((s) => s.name)).toEqual([
        '温度(°C)', '扭矩(N·m)', '角度(rad)', '全关节最高温(°C)',
      ]);
      // 默认选中第 0 个关节（JOINT_NAMES[0]），样例数据里该关节无值 → null
      expect(parsed.series[0].data).toEqual([null, null]);
      // 包络线取后端逐桶聚合的 max_joint_temp（66.2 / 67.8），与选中关节无关
      expect(parsed.series[3].data).toEqual([66.2, 67.8]);
    });
  });

  it('选中关节切换后取该关节的 joint_positions（回归：曾整条角度线恒空）', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    // Heatmap 关节块的 aria-label 以关节全名为前缀（如 "left_elbow_pitch 62.5°C"）
    const chip = await screen.findByRole('button', { name: /^left_elbow_pitch/ });
    await user.click(chip);

    await waitFor(async () => {
      const parsed = await readChartOption();
      // left_elbow_pitch 在样例两点里分别为 60.0/62.5（temp）、1.5/1.7（torque）、0.42/0.55（position）
      expect(parsed.series[0].data).toEqual([60, 62.5]);
      expect(parsed.series[1].data).toEqual([1.5, 1.7]);
      expect(parsed.series[2].data).toEqual([0.42, 0.55]);
      // 切关节不影响包络线（max_joint_temp 与选中关节无关）
      expect(parsed.series[3].data).toEqual([66.2, 67.8]);
    });
  });

  it('history 为空时显示「无历史点」而不是空图', async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse({ robot_id: 'humanoid-042', points: [] }),
    );
    openDrawer();
    render(<RobotDetailDrawer />);

    expect(await screen.findByText(/无历史点/)).toBeInTheDocument();
    // 关节图与电量趋势图共用同一份历史——空即两块都不得挂载
    expect(screen.queryAllByTestId('echarts-option')).toHaveLength(0);
  });

  it('history 请求 500 时显示错误态，且错误文案含 HTTP 状态码（便于排障）', async () => {
    fetchMock.mockImplementation(async () => new Response(null, { status: 500 }));
    openDrawer();
    render(<RobotDetailDrawer />);

    // 5xx 会走 queryClient 的重试（lib/queryClient.ts:27：4xx 不重试，其余重试到 2 次），
    // retryDelay = min(1000 * 2^attempt, 8000) → 1s + 2s ≈ 3s 后才落到 error 态。
    // 默认 findByText 只有 1s，会在重试途中判失败。这里显式放宽到 6s（留足余量），
    // 断言内容不变 —— 仍要求错误文案带 HTTP 状态码。
    expect(
      await screen.findByText(/历史数据不可用/, undefined, { timeout: 6000 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
  }, 10_000);

  it('关闭按钮把 selectedRobotId 置空（Drawer 卸载）', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    await user.click(screen.getByRole('button', { name: '关闭' }));
    expect(useRobotStore.getState().selectedRobotId).toBeNull();
  });

  it('请求必须带 Authorization 头（订单 15：唯一的 REST 调用点，漏了就全漏）', async () => {
    const seen: (string | null)[] = [];
    fetchMock.mockImplementation(async (_input, init) => {
      seen.push(new Headers(init?.headers).get('authorization'));
      return jsonResponse(SAMPLE_HISTORY);
    });
    // 预置 token（sessionStorage）
    window.sessionStorage.setItem('robotmonitor.jwt', 'test-token');

    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen[0]).toBe('Bearer test-token');
  });

  it('告警区按 robot_id 过滤：只显示本机告警，升级标记可见', async () => {
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/alerts')) return jsonResponse(SAMPLE_ALERT_PAGE);
      return jsonResponse(SAMPLE_HISTORY);
    });
    openDrawer();
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    // humanoid-042 的 motor_temp 告警渲染（ruleLabel 复用 AlertLogTable 的映射）
    expect(await within(aside).findByText('电机过热')).toBeInTheDocument();
    expect(within(aside).getByText('已升级')).toBeInTheDocument();
    // humanoid-007 的低电量告警不得串台（规则标签与 message 两个出现点都验）
    expect(within(aside).queryByText('低电量')).not.toBeInTheDocument();
    expect(within(aside).queryByText('电量低')).not.toBeInTheDocument();
  });
});
