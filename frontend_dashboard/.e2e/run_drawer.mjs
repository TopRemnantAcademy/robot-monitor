// Verify the Drawer: GET /robots/{id}/history?minutes=5&resolution=10s → ECharts has data.
import { JSDOM } from 'jsdom';
import http from 'node:http';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
for (const k of ['DOMException','HTMLElement','Element','MessageEvent','Event','Node','SVGElement','getComputedStyle','requestAnimationFrame','cancelAnimationFrame','MutationObserver']) {
  if (dom.window[k]) global[k] = dom.window[k];
}
global.IS_REACT_ACT_ENVIRONMENT = false;
// jsdom has no canvas 2D context; ECharts would spam the console. Only mute test-environment noise; assertions are unaffected.
const origErr = console.error.bind(console);
console.error = (...a) => {
  const msg = String(a[0] ?? '');
  if (msg.includes("Can't get DOM width or height") || msg.includes('_onframe') || msg.includes('Not implemented')) return;
  origErr(...a);
};

// Fake backend: record the requested URL, return 30 history points at 10s resolution.
let requestedUrl = null;
const ROUTE = '/robots/humanoid-042/history?minutes=5&resolution=10s';
const server = http.createServer((req, res) => {
  requestedUrl = req.url;
  if (req.url.startsWith('/robots/humanoid-042/history')) {
    const points = Array.from({ length: 30 }, (_, i) => ({
      time: new Date(Date.now() - (29 - i) * 10_000).toISOString(),
      battery_pct: 20 + i * 0.1,
      max_joint_temp: 60 + i * 0.5,
      joint_temperatures: { left_shoulder_pitch: 50 + i },
      joint_torques: { left_shoulder_pitch: 1.5 },
      joint_positions: { left_shoulder_pitch: 0.11 },
    }));
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ robot_id: 'humanoid-042', points }));
  } else { res.statusCode = 404; res.end('{}'); }
});
await new Promise((r) => server.listen(8201, r));

// require patch: echarts-for-react is a CJS single default export,
// so under Node require yields { default: fn }; unwrap here so React doesn't receive an object (test environment only).
import { createRequire } from 'node:module';
const req = createRequire(import.meta.url);
// Order 09: the product switched to the core entry; the same interop problem exists on 'echarts-for-react/lib/core',
// so both entries must be unwrapped (order doesn't matter; each is judged independently).
for (const id of ['echarts-for-react', 'echarts-for-react/lib/core']) {
  const mod = req(id);
  if (mod && typeof mod === 'object' && typeof mod.default === 'function') {
    req.cache[req.resolve(id)].exports = mod.default;
  }
}

const { __MOUNT__ } = await import('./drawer.cjs');
window.__MOUNT__();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(1500);

const html = document.body.innerHTML;
// Count: 24 joint blocks (JointHeatmap uses button + title="joint_name · xx.x°C").
const jointTitles = [...html.matchAll(/title="([a-z_]+) · ([\d.]+)°C"/g)].map((m) => m[1]);
const blocks = (html.match(/<button[^>]*title="[a-z_]+ · /g) || []).length;
console.log('Heatmap block button count:', blocks, '(expected 24)');
console.log('Joint names (first 6):', jointTitles.slice(0, 6).join(', '));
const out = {
  requestedUrl,
  hasHeatmap: html.includes('Joint temperature array (24 joints, telemetry schema)') && html.includes('°C'),
  heatCellCount: (html.match(/°C/g) || []).length,
  hasEchartsCanvas: html.includes('<canvas') || html.includes('echarts'),
  hasRobotId: html.includes('humanoid-042'),
  hasBattery: html.includes('18.4%'),
  hasError: html.includes('History unavailable'),
  hasNoData: html.includes('No history points'),
};
console.log('Requested URL     :', out.requestedUrl, '(expected to contain minutes=5&resolution=10s)');
console.log('Drawer has robot ID:', out.hasRobotId);
console.log('Basic-info battery 18.4%:', out.hasBattery);
console.log('Heatmap rendered  :', out.hasHeatmap);
console.log('ECharts container :', out.hasEchartsCanvas);
console.log('Error / no data   :', out.hasError, '/', out.hasNoData);

// Reverse-lookup the real option from the echarts-for-react instance to confirm the line chart got 30 history points.
// The echarts instance hangs off the DOM attribute _echarts_instance_; read the option via echarts' own registry.
// Order 09: the product registers via `echarts/core` (a separate instance registry from the full `echarts`),
// so we must read through the same entry, otherwise getInstanceByDom is always null (test-side coupling, not a product defect).
const echartsMod = req('echarts/core');
const marked = [...document.querySelectorAll('[_echarts_instance_]')];
let seriesLen = null, xLen = null;
for (const el of marked) {
  const inst = echartsMod.getInstanceByDom(el);
  if (inst) { const o = inst.getOption(); seriesLen = o.series?.length ?? null; xLen = o.xAxis?.[0]?.data?.length ?? null; break; }
}
console.log('ECharts series count:', seriesLen, '(expected 3: temp/torque/angle)');
console.log('ECharts x-axis points:', xLen, '(expected 30)');

const checks = [
  ['ECharts received 3 series', seriesLen === 3, 'n=' + seriesLen],
  ['ECharts x-axis = 30 10s history points', xLen === 30, 'n=' + xLen],
  ['REST path = /robots/{id}/history?minutes=5&resolution=10s', requestedUrl === ROUTE, requestedUrl],
  ['ECharts line rendered (canvas present)', out.hasEchartsCanvas, ''],
  ['24 joint heatmap blocks rendered', blocks === 24, 'blocks=' + blocks],
  ['Joint names match spec/00 §5.5', jointTitles.length === 24 && jointTitles[0] === 'left_shoulder_pitch' && jointTitles[23] === 'right_ankle_roll', jointTitles[0] + '…' + jointTitles[23]],
  ['Heatmap blocks contain temperature values', out.heatCellCount > 0, 'n=' + out.heatCellCount],
  ['No request error', !out.hasError && !out.hasNoData, ''],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, ex ? `[${ex}]` : ''); if (!ok) fail++; }
server.close(); process.exit(fail ? 1 : 0);
