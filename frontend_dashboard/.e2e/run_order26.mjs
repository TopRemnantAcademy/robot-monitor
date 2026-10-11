// 订单 26（I-20）验证：ErrorBoundary 兜底 + react-query 缓存/重试
// 前置：E2E_API_BASE=http://localhost:8202 node .e2e/build.mjs
import { JSDOM } from 'jsdom';
import http from 'node:http';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true, url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
for (const k of ['DOMException','HTMLElement','Element','MessageEvent','Event','Node','SVGElement','getComputedStyle','requestAnimationFrame','cancelAnimationFrame','MutationObserver','sessionStorage']) {
  if (dom.window[k]) global[k] = dom.window[k];
}
global.IS_REACT_ACT_ENVIRONMENT = false;
// React 对边界捕获必然 console.error；断言看的是 DOM，静音以保持输出干净
const origErr = console.error.bind(console);
console.error = (...a) => {
  const msg = String(a[0] ?? '');
  if (msg.includes('模拟渲染异常') || msg.includes('The above error occurred') || msg.includes('Not implemented')) return;
  origErr(...a);
};

// 假后端：/history 前 2 次 500（触发重试），第 3 次 200
let historyHits = 0;
const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.url.startsWith('/robots/humanoid-042/history')) {
    historyHits++;
    if (historyHits <= 2) { res.statusCode = 500; res.end('{}'); return; }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ robot_id: 'humanoid-042', points: [
      { time: new Date().toISOString(), battery_pct: 20, max_joint_temp: 60, joint_temperatures: { a: 1 }, joint_torques: { a: 1 } },
    ]}));
    return;
  }
  res.statusCode = 404; res.end('{}');
});
await new Promise((r) => server.listen(8202, r));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const checks = [];
const check = (n, ok, ex = '') => { checks.push([n, ok, ex]); console.log(ok ? '  ✅' : '  ❌', n, ex ? `[${ex}]` : ''); };

await import('./order26.cjs');
const __MOUNT_BOUNDARY__ = window.__MOUNT_BOUNDARY__;
const __MOUNT_QUERY__ = window.__MOUNT_QUERY__;

// ===== A) ErrorBoundary =====
const __UNMOUNT_BOUNDARY__ = __MOUNT_BOUNDARY__();
await sleep(300);
const bHtml = document.body.innerHTML;
check('子组件抛错时显示兜底 UI（不白屏）', bHtml.includes('看板渲染出错'), '');
check('兜底 UI 含错误摘要', bHtml.includes('模拟渲染异常'), '');
check('兜底 UI 提供「重试」按钮', bHtml.includes('重试') && bHtml.includes('刷新页面'), '');
check('抛错组件内容已卸载', !bHtml.includes('data-testid="ok"'), '');
__UNMOUNT_BOUNDARY__();

// ===== B) react-query 缓存 + 重试 =====
__MOUNT_QUERY__();
await sleep(4000); // 2 次 500 → 退避重试 3 次，留足时间
check('失败后自动重试并最终成功（ok:1）', document.querySelector('[data-testid="state"]')?.textContent === 'ok:1', document.querySelector('[data-testid="state"]')?.textContent);
check('重试确实发生了（后端命中 >=3 次）', historyHits >= 3, 'hits=' + historyHits);

// 缓存：同 key 再发起一次 fetchQuery，staleTime 内应命中缓存、不再打后端
const qc = window.__QC__;
const before = historyHits;
await qc.fetchQuery({ queryKey: ['robot-history', 'humanoid-042'], queryFn: () => Promise.reject(new Error('不该被调用')) });
check('staleTime 内命中缓存（不再打后端）', historyHits === before, `before=${before} after=${historyHits}`);

server.close();
let fail = 0;
for (const [, ok] of checks) if (!ok) fail++;
console.log(fail === 0 ? '\n订单 26 全部通过 ✅' : `\n订单 26 存在失败 ❌ (${fail})`);
process.exit(fail ? 1 : 0);
