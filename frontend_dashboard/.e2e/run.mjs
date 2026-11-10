// Integration verification: real useRobotWebSocket + real Zustand store + jsdom + a fake WebSocket server.
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

// Use the real `ws` package to fake the browser WebSocket (only the readyState constant names differ; behavior is identical).
const OPEN = 1, CONNECTING = 0;
// The ws package already exposes OPEN/CONNECTING static properties; reuse them directly
if (global.window.WebSocket.OPEN !== OPEN) {
  Object.defineProperty(global.window.WebSocket, 'OPEN', { value: OPEN, configurable: true });
}
global.WebSocket = global.window.WebSocket;
global.IS_REACT_ACT_ENVIRONMENT = false;

const require = createRequire(import.meta.url);
const React = require('react');
const ReactDOMClient = require('react-dom/client');

// ---- Fake WS server (simulating the backend /ws/robots) ----
const PORT = 8123;
const wss = new WebSocketServer({ port: PORT });
let sent = 0;
const serverReady = new Promise((res) => wss.on('listening', res));

wss.on('connection', (ws) => {
  // ① snapshot: 100 robots, full fill.
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

  // ② telemetry flood: 100 robots × 10Hz = 1000 msg/s, sustained for 1.2s.
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

  // ③ alert: one (then a second).
  setTimeout(() => {
    ws.send(JSON.stringify({
      type: 'alert',
      data: { robot_id: 'humanoid-007', rule_id: 'motor_temp', severity: 3, message: 'Joint temp 82.3°C exceeds threshold 75°C', ts: Date.now() },
    }));
    ws.send(JSON.stringify({
      type: 'alert',
      data: { robot_id: 'humanoid-013', rule_id: 'battery', severity: 2, message: 'Battery 12% below threshold 15%', ts: Date.now() + 5 },
    }));
  }, 300);
});

await serverReady;
console.log(`[mock-ws] listening on ws://localhost:${PORT}/ws/robots`);

// ---- Mount the real React component tree (real hook + store) ----
const { __E2E_MOUNT__ } = await import('./bundle.cjs');
__E2E_MOUNT__ ? __E2E_MOUNT__() : window.__E2E_MOUNT__();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(300);
const afterSnapshot = { ...window.__E2E__, robots: undefined };
console.log('--- After snapshot ---');
console.log('wsStatus          :', afterSnapshot.wsStatus);
console.log('robots filled      :', window.__E2E__.robots.size);
const r7 = window.__E2E__.robots.get('humanoid-007');
console.log('humanoid-007 state :', r7.status_code, 'temp 30+6=', r7.max_joint_temp);

const rendersAfterSnapshot = window.__E2E__.renderCount;
await sleep(1500);
const final = window.__E2E__;
console.log('--- After 1000 msg/s flood for 1.2s ---');
console.log('messages sent      :', sent);
console.log('store robot count  :', final.robots.size);
console.log('renders during flood:', final.renderCount - rendersAfterSnapshot, '(≈ 1200ms/100ms ≈ 12, not 1000+)');
console.log('alert count        :', final.alerts.length);
console.log('alerts on top (newest first):', final.alerts.map((a) => a.robot_id).join(', '));
console.log('humanoid-001 latest temp:', final.robots.get('humanoid-001').max_joint_temp);

// ---- Assertions ----
const a = [];
const ck = (name, cond, extra = '') => { a.push({ name, ok: !!cond, extra }); };
ck('snapshot full-fills 100 robots', afterSnapshot.robots ? true : window.__E2E__.robots.size === 100, 'size=' + window.__E2E__.robots.size);
ck('wsStatus=open', final.wsStatus === 'open', final.wsStatus);
ck('render count under 1000 msg/s ≤ 30 (per-message setState would be 1000+)', final.renderCount - rendersAfterSnapshot <= 30, 'renders=' + (final.renderCount - rendersAfterSnapshot));
ck('telemetry increment persisted (temp overwritten to 72.5 by flood)', final.robots.get('humanoid-001').max_joint_temp === 72.5, String(final.robots.get('humanoid-001').max_joint_temp));
ck('alert prepended to the alert stream (2 entries)', final.alerts.length === 2, 'n=' + final.alerts.length);
ck('newest alert is first (battery arrived later → first)', final.alerts[0]?.rule_id === 'battery', final.alerts[0]?.rule_id);
ck('messages sent to server ≥ 1000 (confirms 1000 msg/s)', sent >= 1000, 'sent=' + sent);

let fail = 0;
for (const x of a) { console.log(x.ok ? '  ✅' : '  ❌', x.name, x.extra ? `[${x.extra}]` : ''); if (!x.ok) fail++; }
wss.close();
process.exit(fail ? 1 : 0);
