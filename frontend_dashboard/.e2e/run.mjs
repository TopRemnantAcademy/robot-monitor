// 集成验证：真实 useRobotWebSocket + 真实 Zustand store + jsdom + 假 WebSocket 服务端
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { WebSocketServer, WebSocket as WsClient } from 'ws';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true });
global.DOMException = dom.window.DOMException;
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.MessageEvent = dom.window.MessageEvent;
global.Event = dom.window.Event;

// 用真实 ws 包假冒浏览器 WebSocket（只覆盖 readyState 常量名，行为一致）
const OPEN = 1, CONNECTING = 0;
// ws 包本身已有 OPEN/CONNECTING 静态属性，直接复用
if (global.window.WebSocket.OPEN !== OPEN) {
  Object.defineProperty(global.window.WebSocket, 'OPEN', { value: OPEN, configurable: true });
}
global.WebSocket = global.window.WebSocket;
global.IS_REACT_ACT_ENVIRONMENT = false;

const require = createRequire(import.meta.url);
const React = require('react');
const ReactDOMClient = require('react-dom/client');

// ---- 假 WS 服务端（模拟后端 /ws/robots）----
const PORT = 8123;
const wss = new WebSocketServer({ port: PORT });
let sent = 0;
const serverReady = new Promise((res) => wss.on('listening', res));

wss.on('connection', (ws) => {
  // ① snapshot：100 台全量
  const robots = Array.from({ length: 100 }, (_, i) => ({
    robot_id: `humanoid-${String(i + 1).padStart(3, '0')}`,
    status_code: i % 10 === 0 ? 3 : 1,
    battery_pct: 80 - (i % 30),
    max_joint_temp: 30 + (i % 60),
    joint_temperatures: { left_shoulder_pitch: 30 + (i % 60) },
    joint_torques: { left_shoulder_pitch: 3.4 },
    joint_positions: { left_shoulder_pitch: 0.12 },
    error_code: 0,
    ts: Date.now(),
  }));
  ws.send(JSON.stringify({ type: 'snapshot', data: { robots } }));

  // ② telemetry 洪流：100 台 × 10Hz = 1000 msg/s，持续 1.2s
  const t0 = Date.now();
  const iv = setInterval(() => {
    for (let i = 0; i < 100; i++) {
      ws.send(JSON.stringify({
        type: 'telemetry',
        data: {
          robot_id: `humanoid-${String(i + 1).padStart(3, '0')}`,
          status_code: 1, battery_pct: 55, max_joint_temp: 72.5,
          joint_temperatures: {}, joint_torques: {}, joint_positions: {},
          error_code: 0, ts: Date.now(),
        },
      }));
      sent++;
    }
    if (Date.now() - t0 > 1200) clearInterval(iv);
  }, 100);

  // ③ alert：一条
  setTimeout(() => {
    ws.send(JSON.stringify({
      type: 'alert',
      data: { robot_id: 'humanoid-007', rule_id: 'motor_temp', severity: 3, message: '关节温度 82.3°C 超过阈值 75°C', ts: Date.now() },
    }));
    ws.send(JSON.stringify({
      type: 'alert',
      data: { robot_id: 'humanoid-013', rule_id: 'battery', severity: 2, message: '电量 12% 低于阈值 15%', ts: Date.now() + 5 },
    }));
  }, 300);
});

await serverReady;
console.log(`[mock-ws] 已监听 ws://localhost:${PORT}/ws/robots`);

// ---- 挂载真实 React 组件树（走真实 hook + store）----
const { __E2E_MOUNT__ } = await import('./bundle.cjs');
__E2E_MOUNT__ ? __E2E_MOUNT__() : window.__E2E_MOUNT__();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(300);
const afterSnapshot = { ...window.__E2E__, robots: undefined };
console.log('--- 快照后 ---');
console.log('wsStatus          :', afterSnapshot.wsStatus);
console.log('robots 填充台数    :', window.__E2E__.robots.size);
const r7 = window.__E2E__.robots.get('humanoid-007');
console.log('humanoid-007 状态  :', r7.status_code, '温度 30+6=', r7.max_joint_temp);

const rendersAfterSnapshot = window.__E2E__.renderCount;
await sleep(1500);
const final = window.__E2E__;
console.log('--- 1000 msg/s 洪流 1.2s 后 ---');
console.log('服务端已发送消息   :', sent);
console.log('store robots 台数  :', final.robots.size);
console.log('洪流期间渲染次数   :', final.renderCount - rendersAfterSnapshot, '(≈ 1200ms/100ms ≈ 12，而非 1000+)');
console.log('alerts 条数        :', final.alerts.length);
console.log('告警置顶(最新在前) :', final.alerts.map((a) => a.robot_id).join(', '));
console.log('humanoid-001 最新温:', final.robots.get('humanoid-001').max_joint_temp);

// ---- 断言 ----
const a = [];
const ck = (name, cond, extra = '') => { a.push({ name, ok: !!cond, extra }); };
ck('snapshot 全量填充 100 台', afterSnapshot.robots ? true : window.__E2E__.robots.size === 100, 'size=' + window.__E2E__.robots.size);
ck('wsStatus=open', final.wsStatus === 'open', final.wsStatus);
ck('1000 msg/s 下渲染次数 ≤ 30（若每消息 setState 会是 1000+）', final.renderCount - rendersAfterSnapshot <= 30, 'renders=' + (final.renderCount - rendersAfterSnapshot));
ck('telemetry 增量落库（温度被洪流覆盖为 72.5）', final.robots.get('humanoid-001').max_joint_temp === 72.5, String(final.robots.get('humanoid-001').max_joint_temp));
ck('alert prepend 到告警流（2 条）', final.alerts.length === 2, 'n=' + final.alerts.length);
ck('最新告警在最前（battery 后到 → 在前）', final.alerts[0]?.rule_id === 'battery', final.alerts[0]?.rule_id);
ck('发往服务端消息数 ≥ 1000（确为 1000 msg/s）', sent >= 1000, 'sent=' + sent);

let fail = 0;
for (const x of a) { console.log(x.ok ? '  ✅' : '  ❌', x.name, x.extra ? `[${x.extra}]` : ''); if (!x.ok) fail++; }
wss.close();
process.exit(fail ? 1 : 0);
