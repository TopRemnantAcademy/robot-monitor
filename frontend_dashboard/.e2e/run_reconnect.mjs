// 验证：WS 断开后指数退避重连（1s→2s→…→10s）+ 连接状态点变红
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
console.log('初次连接数      :', connections, '状态:', window.__E2E__.wsStatus);

// 服务端主动踢断，触发前端退避重连
wss.clients.forEach((c) => c.terminate());
await sleep(150);
console.log('断开后 wsStatus :', window.__E2E__.wsStatus, '(期望 closed = 红点)');

// 每轮：等重连成功 → 再踢断，观察退避递增（1s → 2s → 4s）
for (let round = 0; round < 4; round++) {
  const before = connections;
  const t0 = Date.now();
  // 等本轮重连成功
  while (connections === before && Date.now() - t0 < 12000) await sleep(50);
  const waited = Date.now() - t0;
  console.log(`  第 ${round + 1} 次重连耗时 ≈ ${waited}ms, 连接数=${connections}`);
  await sleep(80);
  wss.clients.forEach((c) => c.terminate()); // 再次踢断，触发下一轮退避
}
const gaps = connectTimes.slice(1).map((t, i) => t - connectTimes[i]);
console.log('重连次数        :', connections - 1);
console.log('重连间隔(ms)    :', gaps.join(', '), '(期望 1000 → 2000 → 4000 → 8000)');
// 期望退避 1s→2s→4s→8s（各留 ±40% 网络/定时抖动余量）
const expect = [1000, 2000, 4000, 8000];
const ok = window.__E2E__.wsStatus === "open"
  && connections >= 5
  && expect.every((v, i) => gaps[i] >= v * 0.6 && gaps[i] <= v * 1.6);
console.log(ok ? '  ✅ 指数退避重连生效' : '  ❌ 退避异常');
wss.close(); process.exit(ok ? 0 : 1);
