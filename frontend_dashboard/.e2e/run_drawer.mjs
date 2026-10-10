// 验证 Drawer：GET /robots/{id}/history?minutes=5&resolution=10s → ECharts 有数据
import { JSDOM } from 'jsdom';
import http from 'node:http';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
for (const k of ['DOMException','HTMLElement','Element','MessageEvent','Event','Node','SVGElement','getComputedStyle','requestAnimationFrame','cancelAnimationFrame','MutationObserver']) {
  if (dom.window[k]) global[k] = dom.window[k];
}
global.IS_REACT_ACT_ENVIRONMENT = false;
// jsdom 无 canvas 2D 上下文，ECharts 会刷屏；只静音测试环境噪音，不影响断言
const origErr = console.error.bind(console);
console.error = (...a) => {
  const msg = String(a[0] ?? '');
  if (msg.includes("Can't get DOM width or height") || msg.includes('_onframe') || msg.includes('Not implemented')) return;
  origErr(...a);
};

// 假后端：记录请求 URL，返回 30 个 10s 历史点
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

// require patch：echarts-for-react 为 CJS 单一 default 导出，
// 在 Node 下 require 得到 { default: fn }，这里解包以免 React 收到 object（仅测试环境需要）
import { createRequire } from 'node:module';
const req = createRequire(import.meta.url);
// 订单 09：产品改用 core 入口，同一互操作问题在 'echarts-for-react/lib/core' 上同样存在，
// 两个入口都要解包（顺序无关，各自独立判断）。
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
// 计数：24 个关节色块（JointHeatmap 用 button + title="关节名 · xx.x°C"）
const jointTitles = [...html.matchAll(/title="([a-z_]+) · ([\d.]+)°C"/g)].map((m) => m[1]);
const blocks = (html.match(/<button[^>]*title="[a-z_]+ · /g) || []).length;
console.log('热力块 button 数 :', blocks, '(期望 24)');
console.log('关节名(前6)      :', jointTitles.slice(0, 6).join(', '));
const out = {
  requestedUrl,
  hasHeatmap: html.includes('关节温度阵列') && html.includes('°C'),
  heatCellCount: (html.match(/°C/g) || []).length,
  hasEchartsCanvas: html.includes('<canvas') || html.includes('echarts'),
  hasRobotId: html.includes('humanoid-042'),
  hasBattery: html.includes('18.4%'),
  hasError: html.includes('历史数据不可用'),
  hasNoData: html.includes('无历史点'),
};
console.log('请求到的 URL     :', out.requestedUrl, '(期望含 minutes=5&resolution=10s)');
console.log('Drawer 含机器人ID :', out.hasRobotId);
console.log('基本信息电量 18.4%:', out.hasBattery);
console.log('热力图已渲染      :', out.hasHeatmap);
console.log('ECharts 容器      :', out.hasEchartsCanvas);
console.log('报错/无数据       :', out.hasError, '/', out.hasNoData);

// 从 echarts-for-react 实例反查真实 option，确保折线拿到 30 个历史点
// echarts 实例挂在 DOM 属性 _echarts_instance_ 上；用 echarts 模块自身的注册表读取 option
// 订单 09：产品改用 `echarts/core` 注册（与全量 `echarts` 是彼此独立的实例注册表），
// 故这里必须用同一个入口读取，否则 getInstanceByDom 恒为 null（测试侧耦合，非产品缺陷）。
const echartsMod = req('echarts/core');
const marked = [...document.querySelectorAll('[_echarts_instance_]')];
let seriesLen = null, xLen = null;
for (const el of marked) {
  const inst = echartsMod.getInstanceByDom(el);
  if (inst) { const o = inst.getOption(); seriesLen = o.series?.length ?? null; xLen = o.xAxis?.[0]?.data?.length ?? null; break; }
}
console.log('ECharts series 数 :', seriesLen, '(期望 3：温度/扭矩/角度)');
console.log('ECharts x 轴点数  :', xLen, '(期望 30)');

const checks = [
  ['ECharts 收到 3 条 series', seriesLen === 3, 'n=' + seriesLen],
  ['ECharts x 轴 = 30 个 10s 历史点', xLen === 30, 'n=' + xLen],
  ['REST 路径= /robots/{id}/history?minutes=5&resolution=10s', requestedUrl === ROUTE, requestedUrl],
  ['ECharts 折线渲染（canvas 存在）', out.hasEchartsCanvas, ''],
  ['24 关节热力块渲染', blocks === 24, 'blocks=' + blocks],
  ['关节名符合 spec/00 §5.5', jointTitles.length === 24 && jointTitles[0] === 'left_shoulder_pitch' && jointTitles[23] === 'right_ankle_roll', jointTitles[0] + '…' + jointTitles[23]],
  ['热力块含温度值', out.heatCellCount > 0, 'n=' + out.heatCellCount],
  ['无请求错误', !out.hasError && !out.hasNoData, ''],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, ex ? `[${ex}]` : ''); if (!ok) fail++; }
server.close(); process.exit(fail ? 1 : 0);
