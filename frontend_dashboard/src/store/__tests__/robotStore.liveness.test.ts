// ISSUE #77 回归：在线判定必须来自后端 is_online，而不是 status_code。
//
// 【为什么单独一个文件而不并进组件测试】
// 这一组断言的是 store/选择器的**纯逻辑**：给定一个 robots Map，在线数是多少。
// 它不渲染组件、不需要 QueryClientProvider、不依赖 msw —— 于是它能独立于
// Issue #78（Drawer 测试基座缺 provider）单独验证本单的核心结论。
// 已验证的事实：本文件全绿、`RobotDetailDrawer.test.tsx` 仍 8/8 红 —— 两者互不干扰。

import { describe, expect, it } from 'vitest';
import { offlinePlaceholder, selectStatusCounts, useRobotStore } from '../robotStore';
import { isRobotOnline } from '../../types/robot';
import type { RobotStatus, RobotTelemetry } from '../../types/robot';

/** 造一份 telemetry：status_code 是**机器人自述**的最后一帧，is_online 是**后端**的判定。 */
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

describe('ISSUE #77 · selectStatusCounts 只认 is_online', () => {
  it('停报但最后一帧"正常"的机器人必须计入离线（旧实现会把它算作在线）', () => {
    const now = Math.floor(Date.now() / 1000);
    const robots = new Map<string, RobotTelemetry>([
      // 1 小时前见过，之后停了；它自述的 status_code 停在 1（正常）
      ['humanoid-001', telemetry('humanoid-001', 1, false, now - 3600)],
      // 正在上报
      ['humanoid-002', telemetry('humanoid-002', 3, true, now)],
    ]);

    const counts = selectStatusCounts({ robots });
    expect(counts.total).toBe(2);
    expect(counts.online).toBe(1);
    expect(counts.offline).toBe(1);
    // 「失联」= 收到过、现在停了 —— 与「从未上报」分开计数（处置动作不同）
    expect(counts.stale).toBe(1);
  });

  it('从未上报的占位卡是离线，且不算失联（首屏语义不得破坏）', () => {
    const robots = new Map<string, RobotTelemetry>([
      ['humanoid-001', offlinePlaceholder('humanoid-001')],
    ]);
    const counts = selectStatusCounts({ robots });
    expect(counts.online).toBe(0);
    expect(counts.offline).toBe(1);
    expect(counts.stale).toBe(0);
    // 占位必须显式声明 is_online=false，而不是让下游靠 status_code===0 去猜
    expect(offlinePlaceholder('humanoid-002').is_online).toBe(false);
    expect(offlinePlaceholder('humanoid-002').last_seen).toBeNull();
  });

  it('反向护栏：旧判据（status_code !== 0）在本场景下会给出不同结论', () => {
    const stale = telemetry('humanoid-001', 1, false, 1_600_000_000);
    const newVerdict = isRobotOnline(stale);
    const oldVerdict = stale.status_code !== 0;
    expect(newVerdict).toBe(false);
    expect(oldVerdict).toBe(true);
    // 两者不等价 ⇒ 上面的断言确实在盯着"依据是哪一个字段"
    expect(newVerdict).not.toBe(oldVerdict);
  });

  it('字段缺失（老后端）时保守判离线，不谎报在线', () => {
    const legacy = { ...telemetry('humanoid-001', 1, false, null) } as Partial<RobotTelemetry>;
    delete legacy.is_online;
    expect(isRobotOnline(legacy as RobotTelemetry)).toBe(false);
  });
});

describe('ISSUE #77 · applySnapshot 不把后端下发的在线标志丢掉', () => {
  it('snapshot 落库后仍保留 is_online / last_seen', () => {
    const now = Math.floor(Date.now() / 1000);
    useRobotStore.getState().applySnapshot([telemetry('humanoid-007', 2, true, now)]);
    const got = useRobotStore.getState().robots.get('humanoid-007');
    expect(got?.is_online).toBe(true);
    expect(got?.last_seen).toBe(now);
    expect(got?.status_code).toBe(2);

    // 快照里没出现的机器人回落到离线占位（首屏 100 张卡的骨架必须还在）
    const missing = useRobotStore.getState().robots.get('humanoid-008');
    expect(missing?.is_online).toBe(false);
  });
});
