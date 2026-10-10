// 订单 24 T2 — RobotDetailDrawer 组件测试（vitest + testing-library + msw）。
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

import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';

import RobotDetailDrawer from '../RobotDetailDrawer';
import { useRobotStore } from '../../store/robotStore';
import { JOINT_NAMES } from '../../types/robot';
import type { RobotTelemetry } from '../../types/robot';
import { server } from '../../test/msw/server';
import { API_BASE, SAMPLE_HISTORY } from '../../test/msw/handlers';

// —— ECharts 替身：把 option 序列化进 DOM，供断言 ——
vi.mock('echarts-for-react/lib/core', () => ({
  default: ({ option }: { option: unknown }) => (
    <pre data-testid="echarts-option">{JSON.stringify(option)}</pre>
  ),
}));

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

async function readChartOption(): Promise<ChartOption> {
  const el = await screen.findByTestId('echarts-option');
  return JSON.parse(el.textContent ?? '{}') as ChartOption;
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

  it('拉取 history 后把 24 关节拆成 温度/扭矩/角度 三条 series 交给 ECharts', async () => {
    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(async () => {
      const parsed = await readChartOption();
      expect(parsed.series.map((s) => s.name)).toEqual([
        '温度(°C)', '扭矩(N·m)', '角度(rad)',
      ]);
      // 默认选中第 0 个关节（JOINT_NAMES[0]），样例数据里该关节无值 → null
      expect(parsed.series[0].data).toEqual([null, null]);
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
    });
  });

  it('history 为空时显示「无历史点」而不是空图', async () => {
    server.use(
      http.get(`${API_BASE}/robots/:robotId/history`, () =>
        HttpResponse.json({ robot_id: 'humanoid-042', points: [] }),
      ),
    );
    openDrawer();
    render(<RobotDetailDrawer />);

    expect(await screen.findByText(/无历史点/)).toBeInTheDocument();
    expect(screen.queryByTestId('echarts-option')).not.toBeInTheDocument();
  });

  it('history 请求 500 时显示错误态，且错误文案含 HTTP 状态码（便于排障）', async () => {
    server.use(
      http.get(`${API_BASE}/robots/:robotId/history`, () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );
    openDrawer();
    render(<RobotDetailDrawer />);

    expect(await screen.findByText(/历史数据不可用/)).toBeInTheDocument();
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
  });

  it('关闭按钮把 selectedRobotId 置空（Drawer 卸载）', async () => {
    const user = userEvent.setup();
    openDrawer();
    render(<RobotDetailDrawer />);

    await user.click(screen.getByRole('button', { name: '关闭' }));
    expect(useRobotStore.getState().selectedRobotId).toBeNull();
  });

  it('请求必须带 Authorization 头（订单 15：唯一的 REST 调用点，漏了就全漏）', async () => {
    const seen: (string | null)[] = [];
    server.use(
      http.get(`${API_BASE}/robots/:robotId/history`, ({ request }) => {
        seen.push(request.headers.get('authorization'));
        return HttpResponse.json(SAMPLE_HISTORY);
      }),
    );
    // 预置 token（sessionStorage）
    window.sessionStorage.setItem('robotmonitor.jwt', 'test-token');

    openDrawer();
    render(<RobotDetailDrawer />);

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    expect(seen[0]).toBe('Bearer test-token');
  });
});
