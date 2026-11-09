// 订单 35 T3 — 告警「抑制」呈现的组件测试（vitest + testing-library）。
//
// 钉住三件事（每一条都是"写错就退化"的点）：
//   1. 被抑制行**必须在场**且带「已抑制」标记 + 根因规则的可读 title；
//      「降视觉权重而非删除」是可追溯要求，行消失即视为回归。
//   2. 「显示已抑制」开关默认**关**：主列表请求必须显式带 `suppressed=false`
//      （抑制项不进未确认流）；打开后另发一条 `suppressed=true` 的请求。
//   3. `RobotDetailDrawer` 的本机告警区同样标记 —— 且它**不**过滤 suppressed
//      （排障时要看见"为什么没弹出来"）。
//
// 【网络层：受控 fetch 替身，不用 msw（Issue #78 根因）】
//   happy-dom 会把全局 `Response`/`ReadableStream` 换成自己的实现，而 msw/node
//   拦截器返回的 body 是 Node(undici) 的 ReadableStream —— happy-dom 的
//   `Response.json()` 读不动这条流，200 用例会永久挂起（只读 res.status 的 500
//   用例反而正常）。本仓 vitest 已切回 jsdom（vitest.config.ts 注释有完整实测记录），
//   但为与既有 `RobotDetailDrawer.test.tsx` 保持同一写法（受控替身 + 断言真实 URL），
//   这里继续用受控替身：它返回 Node 原生 `Response`，既绕开环境差异，
//   又能抓住「URL 拼错 / 过滤参数漏传 / 字段改名」这类回归。

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
 * 一条**被抑制**的告警：humanoid-042 的 battery_low(sev2)，
 * 被同机 motor_temp(sev3) 压住 —— 与 alert_engine 的 InhibitionRecord 同形。
 */
const SUPPRESSED_ITEM = {
  alert_id: 21,
  time: '2026-10-10T23:45:00Z',
  robot_id: 'humanoid-042',
  rule_id: 'battery_low',
  severity: 2,
  message: '电量 17.4% 低于 20%',
  detail: { suppressed_reason: 'severity', suppressed_source_rule: 'motor_temp' },
  ack: false,
  ack_at: null,
  ack_by: null,
  escalated: false,
  suppressed: true,
  suppressed_by: 'motor_temp',
};

/** 一条正常的未确认告警（对照组：必须**不被**标记）。*/
const NORMAL_ITEM = {
  alert_id: 22,
  time: '2026-10-10T23:44:00Z',
  robot_id: 'humanoid-042',
  rule_id: 'joint_stuck',
  severity: 2,
  message: '左肩关节卡死',
  detail: null,
  ack: false,
  ack_at: null,
  ack_by: null,
  escalated: false,
  suppressed: false,
  suppressed_by: null,
};

/** 记录每次 /alerts 请求的完整 URL，供「过滤参数」断言。 */
const alertsUrls: string[] = [];

/**
 * 受控 fetch：按 URL 里的 `suppressed` 参数分流。
 * `suppressed=true` → 只有被抑制那条；否则 → 只有正常那条（模拟后端三态过滤）。
 * 这样「开关关着时错发 true / 打开时错发 false」都会立刻被断言抓住。
 */
function urlBehaviour(input: RequestInfo | URL): Response {
  const url = String(input);
  if (!url.includes('/alerts')) return jsonResponse(SAMPLE_HISTORY);
  alertsUrls.push(url);
  const wantSuppressed = new URL(url, 'http://localhost').searchParams.get('suppressed');
  const items = wantSuppressed === 'true' ? [SUPPRESSED_ITEM] : [NORMAL_ITEM];
  return jsonResponse({ items, total: items.length, limit: ALERT_PAGE_SIZE, offset: 0 });
}

// —— ECharts 替身（与 RobotDetailDrawer.test.tsx 同写法）——
// jsdom 没有 canvas，真渲染 ECharts 会抛 `Cannot read properties of null (reading
// 'clearRect')`（本机实测，zrender 的 Layer.clear）。把 option dump 进 DOM 即可：
// 本文件测的是抑制标记，图表怎么画像素不在范围内。
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
 * 每个用例开头复位替身与请求记录（用例之间不共享调用历史）。
 *
 * **刻意不用 `mockReset()`**：reset 会把实现清成 undefined，而 `render()` 之后
 * react-query 的首个查询可能在本次 `arm()` 与下一次 `arm()` 之间仍在途 ——
 * reset 造成"第一次请求返回 undefined.json 崩掉"的假失败（本机实测）。
 * 只换 implementation 即可达到"每例隔离"的目的。
 */
function arm(impl = urlBehaviour): void {
  alertsUrls.length = 0;
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => impl(input));
}

describe('AlertLogTable — 抑制呈现（订单 35）', () => {
  it('开关默认关闭：主列表请求带 suppressed=false，且不渲染被抑制行', async () => {
    arm();
    render(<AlertLogTable />);

    // 关键：主列表**显式**带 suppressed=false（不依赖后端默认值，契约刻意中立）
    await waitFor(() => expect(alertsUrls.length).toBeGreaterThan(0));
    expect(alertsUrls[0]).toContain('suppressed=false');
    // 正常行在场（ruleLabel: joint_stuck → Joint stuck）。
    // 用 waitFor 而非 findByText：react-query 的 resolve → setState → 重渲染
    // 是一条 async 链，findByText 的 1s 默认窗口在本机实测会偶发超时。
    await waitFor(() => expect(screen.getByText('左肩关节卡死')).toBeInTheDocument());
    expect(screen.getByText('Joint stuck · S2')).toBeInTheDocument();
    // 被抑制行不在默认视图里（后端已按 suppressed=false 过滤，前端也不该出现）
    expect(screen.queryByText('⊘ Suppressed')).not.toBeInTheDocument();
    // 开关是 off
    expect(screen.getByRole('switch', { name: /Show suppressed/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('打开「显示已抑制」后另拉 suppressed=true，并在行内标「已抑制」+ 根因 title', async () => {
    const user = userEvent.setup();
    arm();
    render(<AlertLogTable />);

    await user.click(await screen.findByRole('switch', { name: /Show suppressed/ }));

    // 专用查询带 suppressed=true（与主列表的 false 是两条独立请求）
    await waitFor(() => expect(alertsUrls.some((u) => u.includes('suppressed=true'))).toBe(true));

    // 行**在场**（降视觉权重而非删除）且标记可见
    const tag = await screen.findByText('⊘ Suppressed');
    expect(tag).toBeInTheDocument();
    // 归因可读：title 把根因规则翻译为英文标签（motor_temp → Motor overheat）
    expect(tag).toHaveAttribute('title', 'Suppressed by higher-severity alert (root cause: Motor overheat)');
    // 被抑制的行仍在表格里（同一 AlertRow 渲染路径），消息文本可查
    expect(screen.getByText('电量 17.4% 低于 20%')).toBeInTheDocument();
    // 关闭开关回默认态：标记随之消失（但不清空缓存，故不重拉）
    await user.click(screen.getByRole('switch', { name: /Show suppressed/ }));
    await waitFor(() => expect(screen.queryByText('⊘ Suppressed')).not.toBeInTheDocument());
  });

  it('被抑制行只降视觉权重：opacity/grayscale 生效但行不被移除（可追溯）', async () => {
    const user = userEvent.setup();
    arm();
    const { container } = render(<AlertLogTable />);

    await user.click(await screen.findByRole('switch', { name: /Show suppressed/ }));
    const tag = await screen.findByText('⊘ Suppressed');

    // 标记所在按钮的行容器 = 该行的根 div（AlertRow 的 <div> 包着一个 <button>）
    const rowDiv = tag.closest('button')?.parentElement as HTMLElement | null;
    expect(rowDiv).not.toBeNull();
    // 降权重：opacity < 1 且加灰度；**不是** display:none（行必须还在 DOM 里）
    expect(rowDiv!.style.opacity).toBe('0.45');
    expect(rowDiv!.style.filter).toBe('grayscale(1)');
    expect(rowDiv!.style.display).not.toBe('none');
    expect(container.contains(rowDiv)).toBe(true);
  });
});

describe('RobotDetailDrawer — 抑制标记（订单 35）', () => {
  it('本机告警区标记「已抑制」，且**不**过滤 suppressed（排障要看见被压制的行）', async () => {
    arm((input) => {
      const url = String(input);
      if (url.includes('/alerts')) {
        // 抽屉刻意不带 suppressed 参数：要全集，才能回答"为什么没弹出来"
        return jsonResponse({ items: [SUPPRESSED_ITEM, NORMAL_ITEM], total: 2, limit: 200, offset: 0 });
      }
      return jsonResponse(SAMPLE_HISTORY);
    });

    const st = useRobotStore.getState();
    st.applyTelemetryBatch([telemetry()]);
    st.selectRobot('humanoid-042');
    render(<RobotDetailDrawer />);

    const aside = screen.getByRole('complementary');
    // 被抑制行在场 + 标记可读（复用 AlertLogTable 的同一措辞与 token）
    const tag = await within(aside).findByText('⊘ Suppressed');
    expect(tag).toHaveAttribute('title', 'Suppressed by higher-severity alert (root cause: Motor overheat)');
    expect(within(aside).getByText('电量 17.4% 低于 20%')).toBeInTheDocument();
    // 对照组：正常告警 render 但**不带**标记 —— 标记只属于被抑制那条
    expect(within(aside).getByText('左肩关节卡死')).toBeInTheDocument();
    expect(within(aside).getAllByText('⊘ Suppressed')).toHaveLength(1);

    // 抽屉的请求**没有** suppressed 过滤参数（刻意要全集：排障时被压制的行才是重点）。
    // 本轮用例用**自定义** implementation（按端点分流），不再走 `arm()` 的默认
    // `urlBehaviour` —— 因此这条断言不用 `alertsUrls`（那是默认实现里的记录器），
    // 而是从替身调用记录里取实际 URL。Drawer 先发 /history 再发 /alerts，
    // 顺序不由测试决定，故扫描全部调用取那一条 /alerts。
    const calls = fetchMock.mock.calls.map((c) => String(c[0]));
    const alertCall = calls.find((u) => u.includes('/alerts'));
    expect(alertCall, `未发出 /alerts 请求，实际：${JSON.stringify(calls)}`).toBeDefined();
    expect(alertCall).not.toContain('suppressed=');
  });
});
