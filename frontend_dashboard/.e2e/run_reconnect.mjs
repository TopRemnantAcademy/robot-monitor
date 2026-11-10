// Verify: after WS disconnect, exponential-backoff reconnect (1s→2s→…→10s) + the connection status dot turns red.
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { WebSocketServer, WebSocket as WsClient } from 'ws';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.DOMException = dom.window.DOMException; global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element; global.MessageEvent = dom.window.MessageEvent; global.Event = dom.window.Event;
global.window.WebSocket = WsClient; global.WebSocket = WsClient;
global.IS_REACT_ACT_ENVIRONMENT = false;
const require = createRequire(import.meta.url);
require('react'); require('react-dom/client');

const PORT = 8124;
const wss = new WebSocketServer({ port: PORT });
await new Promise((r) => wss.on('listening', r));
let connections = 0;
const connectTimes = [];
wss.on('connection', (ws) => { connections++; connectTimes.push(Date.now()); ws.send(JSON.stringify({ type: 'snapshot', data: { robots: [] } })); });

const { __E2E_MOUNT__ } = await import('./bundle.cjs');
__E2E_MOUNT__ ? __E2E_MOUNT__() : window.__E2E_MOUNT__();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(200);
console.log('Initial connections:', connections, 'state:', window.__E2E__.wsStatus);

// The server actively kicks the connection, triggering the front-end backoff reconnect.
wss.clients.forEach((c) => c.terminate());
await sleep(150);
console.log('wsStatus after disconnect:', window.__E2E__.wsStatus, '(expected closed = red dot)');

// Each round: wait for reconnect success → kick again, observing the backoff increasing (1s → 2s → 4s).
for (let round = 0; round < 4; round++) {
  const before = connections;
  const t0 = Date.now();
  // Wait for this reconnect round to succeed
  while (connections === before && Date.now() - t0 < 12000) await sleep(50);
  const waited = Date.now() - t0;
  console.log(`  Reconnect #${round + 1} took ≈ ${waited}ms, connections=${connections}`);
  await sleep(80);
  wss.clients.forEach((c) => c.terminate()); // Kick it again to trigger the next backoff round
}
const gaps = connectTimes.slice(1).map((t, i) => t - connectTimes[i]);
console.log('Reconnect count     :', connections - 1);
console.log('Reconnect gaps (ms) :', gaps.join(', '), '(expected 1000 → 2000 → 4000 → 8000)');
// Expected backoff 1s→2s→4s→8s (each with ±40% margin for network/timer jitter).
const expect = [1000, 2000, 4000, 8000];
const ok = window.__E2E__.wsStatus === "open"
  && connections >= 5
  && expect.every((v, i) => gaps[i] >= v * 0.6 && gaps[i] <= v * 1.6);
console.log(ok ? '  ✅ Exponential backoff reconnect works' : '  ❌ Backoff abnormal');
wss.close(); process.exit(ok ? 0 : 1);
