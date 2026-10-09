// 集成验证入口 1：WS 接入 + store（jsdom 下挂载真实 hook）
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
  // 记录"因 robots 变化而重渲染"的次数，用于证明 100ms 节流生效
  const [renderCount, setRenderCount] = useState(0);
  useEffect(() => { setRenderCount((c) => c + 1); }, [robots]);
  useEffect(() => {
    window.__E2E__ = { robots, alerts, wsStatus, renderCount, setRenderCount };
  }, [robots, alerts, wsStatus, renderCount]);
  return null;
}

window.__E2E_MOUNT__ = () => createRoot(document.getElementById('root')!).render(<Probe />);
export {};
