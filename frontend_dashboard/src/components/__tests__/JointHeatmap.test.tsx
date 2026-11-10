// JointHeatmap component test (vitest + testing-library).
//
// Mirrors the canonical template src/components/__tests__/RobotDetailDrawer.test.tsx.
// This component takes props (no react-query, no fetch, no echarts) and renders a 24-joint
// color-block grid. We drive it purely through props and assert:
//   - all 24 joint blocks render, with accessibility (aria-label) carrying the joint name + temperature;
//   - block background color reflects the joint temperature;
//   - clicking a block calls the onSelectJoint callback with that joint's name.

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import JointHeatmap from '../JointHeatmap';
import { JOINT_NAMES } from '../../types/robot';
import type { JointDict } from '../../types/robot';

/** Build a 24-joint temperature map (distinct value per joint so we can assert per-block color/value). */
function makeTemps(fn: (i: number) => number): JointDict {
  const map: JointDict = {};
  JOINT_NAMES.forEach((j, i) => {
    map[j] = fn(i);
  });
  return map;
}

describe('JointHeatmap', () => {
  it('Renders exactly 24 joint blocks, each carrying its joint name in the aria-label', () => {
    const temps = makeTemps((i) => i * 3); // 0, 3, 6, …, 69°C — all finite values
    render(<JointHeatmap temperatures={temps} />);

    // 24 interactive blocks (buttons).
    const blocks = screen.getAllByRole('button');
    expect(blocks).toHaveLength(24);

    // Every joint name appears as the start of some block's aria-label.
    for (const joint of JOINT_NAMES) {
      const block = screen.getByRole('button', { name: new RegExp(`^${joint} `) });
      expect(block).toBeInTheDocument();
    }

    // A known joint's aria-label carries its temperature, formatted to 1 decimal.
    const first = screen.getByRole('button', { name: /^left_shoulder_pitch/ });
    expect(first).toHaveAttribute('aria-label', 'left_shoulder_pitch 0.0°C');
  });

  it('Block color reflects the joint temperature (hot block ≠ cold block)', () => {
    // Make the first joint very cold (0°C) and the last joint very hot (90°C).
    const temps = makeTemps((i) => (i === 0 ? 0 : i === JOINT_NAMES.length - 1 ? 90 : 30));
    render(<JointHeatmap temperatures={temps} />);

    const cold = screen.getByRole('button', { name: /^left_shoulder_pitch/ });
    const hot = screen.getByRole('button', { name: /^right_ankle_roll/ });

    const coldBg = (cold as HTMLElement).style.backgroundColor;
    const hotBg = (hot as HTMLElement).style.backgroundColor;
    expect(coldBg).not.toBe('');
    expect(hotBg).not.toBe('');
    expect(hotBg).not.toBe(coldBg); // tempColor gives green for 0°C, red for 90°C
  });

  it('Shows "--" / "No data" for joints with no reported temperature', () => {
    // Empty map → every block is in the no-data state.
    render(<JointHeatmap temperatures={{}} />);

    const block = screen.getByRole('button', { name: /^left_shoulder_pitch/ });
    expect(block).toHaveAttribute('aria-label', 'left_shoulder_pitch No data');
    // The numeric area renders "--" when there is no value (the °C axis is a nested span).
    expect(block.textContent).toContain('--');
  });

  it('Clicking a joint block calls onSelectJoint with that joint name', () => {
    const onSelectJoint = vi.fn();
    const temps = makeTemps((i) => i * 2);
    render(<JointHeatmap temperatures={temps} onSelectJoint={onSelectJoint} />);

    const block = screen.getByRole('button', { name: /^left_elbow_pitch/ });
    fireEvent.click(block);

    expect(onSelectJoint).toHaveBeenCalledTimes(1);
    expect(onSelectJoint).toHaveBeenCalledWith('left_elbow_pitch');
  });

  it('Selected joint gets a highlighted (2px) border', () => {
    const temps = makeTemps((i) => i * 2);
    render(<JointHeatmap temperatures={temps} selectedJoint="left_elbow_pitch" />);

    const selected = screen.getByRole('button', { name: /^left_elbow_pitch/ });
    const other = screen.getByRole('button', { name: /^left_wrist_roll/ });

    expect((selected as HTMLElement).style.borderWidth).toBe('2px');
    expect((other as HTMLElement).style.borderWidth).toBe('1px');
  });
});
