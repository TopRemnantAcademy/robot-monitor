// Integration entry point 1: WS connection + store (mount the real hook under jsdom)
import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { useRobotWebSocket } from '../src/hooks/useRobotWebSocket';
import { useRobotStore } from '../src/store/robotStore';

declare global {
  interface Window { __E2E__: unknown; __E2E_MOUNT__: (() => void) | undefined }
}

function Probe() {
  useRobotWebSocket();
  const robots = useRobotStore((s) => s.robots);
  const alerts = useRobotStore((s) => s.alerts);
  const wsStatus = useRobotStore((s) => s.wsStatus);
  // Count re-renders caused by changes to `robots`, used to prove the 100ms throttle is in effect
  const [renderCount, setRenderCount] = useState(0);
  useEffect(() => { setRenderCount((c) => c + 1); }, [robots]);
  useEffect(() => {
    window.__E2E__ = { robots, alerts, wsStatus, renderCount, setRenderCount };
  }, [robots, alerts, wsStatus, renderCount]);
  return null;
}

window.__E2E_MOUNT__ = () => createRoot(document.getElementById('root')!).render(<Probe />);
export {};
