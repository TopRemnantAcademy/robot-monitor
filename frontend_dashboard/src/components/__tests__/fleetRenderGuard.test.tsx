// 订单 33 T2 —— 机群渲染护栏回归测试（100 台卡片墙 + 健康分析条）。
//
// 【这份测试补的是哪个洞】
// AGENTS.md §3 前端护栏写着「严禁每条 WS 消息触发 React 重渲染，必须 100ms 节流」。
// 订单 16 T2 已落地**在线计数器**（src/lib/observability.ts）—— 但那是「跑起来肉眼看
// console」的观测，**没有任何自动化断言**。于是谁哪天把卡片的自订阅改回订阅整张 Map、
// 或把 FleetHealthAnalytics 的 useShallow 拆掉，`npm test` 依旧全绿，直到大屏卡死
// 才被人发现。本文件把那条护栏钉成可判定的红/绿（订单 33 的「推测：目前没有测试断言
// 100 台卡片墙的渲染次数不随机群规模线性爆炸」经本单证实为真，此处补齐）。
//
// 【测量仪器的选择 —— 这是本文件最需要交代的地方】
// 试过但**放弃** Profiler：React.Profiler 的 onRender 只报告「被包裹子树的提交」，
// 实测「变化 3 台」与「变化 100 台」两帧的 Profiler 回调次数**完全相同**（都是 10，
// 每帧一次）—— 它数不出「几张卡重渲染」。用它会把阈值写得毫无意义（自欺）。
//
// 采用的仪器（两个，各自精确）：
//   1) 卡片级：`vi.mock('../theme/tokens')` 把 `robotDisplayState` 换成 spy。
//      RobotCard 函数体**每次渲染恰好调用它一次**（FleetOverviewCards.tsx:37），
//      故 spy 调用次数 == 卡片渲染次数，精确到 1。挂载时 = 100（100 张卡）。
//   2) 网格体级：observability 的 `cardRenders`（订单 16 T2）——
//      它在 **RobotGrid 本体**自增（FleetOverviewCards.tsx:116-118），数的是「网格
//      容器重渲染了几次」，与逐卡渲染解耦。两者互补，缺一不可。
//
// 【阈值依据 —— 不是拍脑袋】
// 100 台 × 10Hz = 1000 msg/s，被 useRobotWebSocket 的 100ms 节流压成 10 帧/s。
// 一帧里「真正变了几台」（k）决定**应然**渲染上限，与机群规模 N 无关：
//   · 空转帧（本帧条目与 state 同引用）：applyTelemetryBatch 探测到无引用变化，
//     直接返回原 state → zustand 零通知 → 卡片 0 次、网格体 0 次重渲染。
//   · k 台变化的帧：卡片重渲染恰好 k 次（自订阅 s.robots.get(id)，引用变才动）；
//     网格体 0 次（它只订阅 selectedRobotId 与 selectRobot，二者本帧不变）。
// 故断言取**等式/常数**而非「宽松小于」：
//   changed=k, frames=f  ⇒  卡片渲染增量 == k × f（既有实现实测精确命中，见 T1 结论）；
//                         网格体 cardRenders 恒 == 1（仅挂载那次）。
// 若实现退化成订阅整张 Map：网格体 cardRenders 会随帧数增长（== 1 + f），断言 1 立即红；
// 若退化成无 memo/无粒度：卡片渲染增量会 == N × f（1000），断言 k×f 立即红。
//
// 【为什么不用 vi.useFakeTimers】
// 订单 33 禁止项 5。这里也不需要 —— 被测的是「store 提交 → React 提交次数」的因果，
// 与时间无关。直接真实调用 applyTelemetryBatch 制造刷库帧即可。

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

// jsdom 无 canvas，真渲染 ECharts 会抛错（同 RobotDetailDrawer.test.tsx 的处理）。
// 组件本体照常执行 —— 我们要数的是分析条组件的渲染，不是 ECharts 画了几个像素。
vi.mock('echarts-for-react/lib/core', () => ({
  default: () => <div data-testid="echarts-stub" />,
}));

// 卡片级渲染计数器：RobotCard 函数体每次渲染恰好调用 robotDisplayState 一次。
// 用 importOriginal 保留其余 token 导出（颜色/文案断言仍走真实实现）。
vi.mock('../../theme/tokens', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../theme/tokens')>();
  return { ...actual, robotDisplayState: vi.fn(actual.robotDisplayState) };
});

const displayStateSpy = tokens.robotDisplayState as unknown as ReturnType<typeof vi.fn>;

/** 造一帧遥测：给定 id 列表全部置为「在线 + 指定电量/温度」，用于制造确定的引用变化。 */
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

/** 用 Profiler 数子树提交（仅用于「确实重渲染了」这类定性断言）。 */
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

describe('订单 33 T2 · 卡片墙渲染护栏（100 台，变化台数=k，帧数=f）', () => {
  it('基线：挂载时恰好渲染 100 张卡（测量仪器自证 — spy == 卡片数）', () => {
    const { unmount } = render(<FleetOverviewCards />);
    // 100 张卡各调用一次 robotDisplayState。若哪天卡片不再逐张渲染（或仪器失效），这里先红。
    expect(displayStateSpy.mock.calls.length).toBe(100);
    unmount();
  });

  it('卡片渲染增量 == k × f（随变化台数走，不随 N 走）', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    // 10 帧，每帧只有 3 台变化
    const k = 3;
    const f = 10;
    const changed = buildRobotIds(100).slice(0, k);
    for (let i = 0; i < f; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(changed, i));
      });
    }

    // 应然：只有这 3 台重渲染，共 3 × 10 = 30 次。
    // 阈值取**等式**：既防「渲染爆炸」（> 30），也防「偷工减料少渲染」（< 30，说明该更新的卡没更新）。
    expect(displayStateSpy.mock.calls.length - afterMount).toBe(k * f);
    unmount();
  });

  it('反向护栏：k=100 全量变化时渲染量 == 100 × f（证明上一条没被"少渲染"糊弄过去）', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    const all = buildRobotIds(100);
    const f = 5;
    for (let i = 0; i < f; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(all, i));
      });
    }

    // 100 台全变 ⇒ 100 张卡必须全部重渲染（这是**正确**的开销，不是护栏要拦的对象）。
    // 本用例的意义：证明卡片确实在按数据变化重渲染 —— 于是上一条的 3×10 只能来自
    // 「只有 3 台变了」，而不是「卡片墙根本不重渲染」这种假绿。
    expect(displayStateSpy.mock.calls.length - afterMount).toBe(100 * f);
    unmount();
  });

  it('空转帧（同引用重复推送）：0 次卡片重渲染（structual sharing 引用探测生效）', () => {
    const { unmount } = render(<FleetOverviewCards />);
    const afterMount = displayStateSpy.mock.calls.length;

    // 先让 3 台有真实对象
    const changed = buildRobotIds(100).slice(0, 3);
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(frame(changed, 0));
    });
    const baseline = displayStateSpy.mock.calls.length;

    // 再把这 3 台**同一批对象**反复推送（模拟重复帧 / 服务端空转）
    const same = changed.map((id) => useRobotStore.getState().robots.get(id)!) as RobotTelemetry[];
    for (let i = 0; i < 10; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(same);
      });
    }

    // applyTelemetryBatch 逐条做引用级探测：全部同引用 → 返回原 state → 零通知。
    expect(displayStateSpy.mock.calls.length).toBe(baseline);
    expect(displayStateSpy.mock.calls.length).toBeGreaterThan(afterMount); // 前面那次真变化仍发生过
    unmount();
  });

  it('网格体本身（RobotGrid）不随刷库帧重渲染：cardRenders 恒 == 1', () => {
    render(<FleetOverviewCards />);
    // 挂载即 1（网格体渲染一次建 100 张卡）
    expect(getSnapshot().cardRenders).toBe(1);

    const all = buildRobotIds(100);
    for (let i = 0; i < 20; i += 1) {
      act(() => {
        useRobotStore.getState().applyTelemetryBatch(frame(all, i));
      });
    }

    // 20 帧全量刷库后网格体渲染数**不变**：它只订阅 selectedRobotId / selectRobot，
    // 这俩在刷库期间引用不变 → zustand 不发通知。若有人把它改回订阅整张 Map，
    // cardRenders 会变成 1 + 20 = 21，本断言立刻红。
    expect(getSnapshot().cardRenders).toBe(1);
  });

  it('选中态变化时网格体确实重渲染（证明 cardRenders 仪器不是恒 1 的摆设）', () => {
    render(<FleetOverviewCards />);
    const before = getSnapshot().cardRenders;

    act(() => {
      useRobotStore.getState().selectRobot('humanoid-042');
    });

    // 网格体订阅 selectedRobotId，选中态变化必须重渲染（用于重算 100 张卡的 selected）。
    expect(getSnapshot().cardRenders).toBeGreaterThan(before);
  });
});

describe('订单 33 T2 · 健康分析条渲染护栏（分桶计数选择器 + useShallow）', () => {
  it('分桶计数不变时组件不重渲染（数据在变、桶不变 ⇒ 浅比较拦住）', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <FleetHealthAnalytics />);
    const afterMount = commits.count;

    // 100 台全部在线、温度恒 30°C、电量恒 90 —— 每帧对象引用都变，但
    // 温度桶恒 [0,0,0,0,100]、电量桶恒 [0,0,0,0,100]、状态桶恒 [100,0,0,0,0]。
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
            max_joint_temp: 30 + ((i + k) % 3), // 恒 < 40 → 桶不变
          })) as RobotTelemetry[],
        );
      });
    }

    // useShallow 逐元素比较三个分桶结果：全等 ⇒ 无通知 ⇒ 组件体提交次数不增长。
    // 阈值给 +1 余量：React 可能在消息/副作用下多提交一次，但不该随帧数增长。
    expect(commits.count).toBeLessThanOrEqual(afterMount + 1);
  });

  it('分桶计数变化时必须重渲染（防止上一条因"组件不订阅"而假绿）', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <FleetHealthAnalytics />);
    const afterMount = commits.count;

    const ids = buildRobotIds(100);
    // 帧 1：温度 30°C → 温度桶 [100,0,0,0,0]
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(
        ids.map((id) => ({
          ...offlinePlaceholder(id), status_code: 1, is_online: true, battery_pct: 90, max_joint_temp: 30,
        })) as RobotTelemetry[],
      );
    });
    // 帧 2：温度 75°C → 温度桶翻到 [0,0,0,0,100]，桶计数真变化
    act(() => {
      useRobotStore.getState().applyTelemetryBatch(
        ids.map((id) => ({
          ...offlinePlaceholder(id), status_code: 1, is_online: true, battery_pct: 90, max_joint_temp: 75,
        })) as RobotTelemetry[],
      );
    });

    // 桶计数从 [100,0,0,0,0] 变 [0,0,0,0,100] ⇒ 必须重渲染，浅比较不能把真变化吃掉。
    expect(commits.count).toBeGreaterThan(afterMount);
  });
});

describe('订单 33 T2 · 顶栏 KPI 渲染护栏（派生统计 + useShallow，浮点抖动不穿透）', () => {
  it('派生统计（在线率/平均电量，1 位小数）不变时顶栏不重渲染', () => {
    const commits = makeCommitCounter();
    renderWithProfiler(commits.onRender, <HealthSummaryHeader />);
    const afterMount = commits.count;

    // 100 台全在线、电量恒 80（平均 80.0）、无告警 → 顶栏显示值恒定。
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

    // 选择器把 onlineRate / avgBattery 四舍五入到 1 位小数（与 toFixed(1) 显示精度一致），
    // 于是纯浮点抖动（80.00000001 之类）不会穿透浅比较。10 帧后提交次数不应增长。
    expect(commits.count).toBeLessThanOrEqual(afterMount + 1);
  });
});
