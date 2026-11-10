// Tests for the WS connection-state badge (GitHub issue #14).
//
// The live WebSocket already reconnects with exponential backoff (useRobotWebSocket.ts); what was
// missing was a visible, screen-reader-announced connection-state badge and a test for the store
// transition that drives it. These cases lock that in.

import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, screen } from '@testing-library/react';
import HealthSummaryHeader from '../HealthSummaryHeader';
import { useRobotStore } from '../../store/robotStore';
import { render as customRender } from '../../test/render';

beforeEach(() => {
  useRobotStore.setState({ wsStatus: 'open', alerts: [], robots: new Map() });
});

afterEach(() => {
  cleanup();
  useRobotStore.setState({ wsStatus: 'open' });
});

describe('connection-state badge', () => {
  it('shows Connected (green) when the socket is open', () => {
    const { container } = customRender(<HealthSummaryHeader />);
    const status = container.querySelector('[role="status"]') as HTMLElement;
    expect(status).not.toBeNull();
    expect(status.getAttribute('aria-label')).toBe('WebSocket Connected');
    // The decorative dot is hidden from the a11y tree; the text carries the state.
    expect(screen.getByText('WS Connected')).toBeTruthy();
  });

  it('shows Connecting when the socket is connecting', () => {
    act(() => useRobotStore.getState().setWsStatus('connecting'));
    const { container } = customRender(<HealthSummaryHeader />);
    const status = container.querySelector('[role="status"]') as HTMLElement;
    expect(status.getAttribute('aria-label')).toBe('WebSocket Connecting');
    expect(screen.getByText('WS Connecting')).toBeTruthy();
  });

  it('transitions the badge when wsStatus changes (connection store transition)', () => {
    const { container } = customRender(<HealthSummaryHeader />);
    let status = container.querySelector('[role="status"]') as HTMLElement;
    expect(status.getAttribute('aria-label')).toBe('WebSocket Connected');

    act(() => useRobotStore.getState().setWsStatus('closed'));
    status = container.querySelector('[role="status"]') as HTMLElement;
    expect(status.getAttribute('aria-label')).toBe('WebSocket Reconnecting');
    expect(screen.getByText('WS Reconnecting')).toBeTruthy();
  });
});
