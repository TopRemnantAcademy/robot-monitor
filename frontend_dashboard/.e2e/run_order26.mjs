// Order 26 (I-20) verification: ErrorBoundary fallback + react-query cache/retry.
// Prerequisite: E2E_API_BASE=http://localhost:8202 node .e2e/build.mjs
import { JSDOM } from 'jsdom';
import http from 'node:http';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true, url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
for (const k of ['DOMException','HTMLElement','Element','MessageEvent','Event','Node','SVGElement','getComputedStyle','requestAnimationFrame','cancelAnimationFrame','MutationObserver','sessionStorage']) {
  if (dom.window[k]) global[k] = dom.window[k];
}
global.IS_REACT_ACT_ENVIRONMENT = false;
// React will console.error on a caught boundary; assertions look at the DOM, so mute to keep output clean.
const origErr = console.error.bind(console);
console.error = (...a) => {
  const msg = String(a[0] ?? '');
  if (msg.includes('simulated render exception') || msg.includes('The above error occurred') || msg.includes('Not implemented')) return;
  origErr(...a);
};

// Fake backend: /history returns 500 for the first 2 calls (triggering retries), then 200 on the 3rd.
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
check('Fallback UI shown when child throws (no white screen)', bHtml.includes('Dashboard render error'), '');
check('Fallback UI contains the error summary', bHtml.includes('simulated render exception'), '');
check('Fallback UI offers a "Retry" button', bHtml.includes('Retry') && bHtml.includes('Reload page'), '');
check('Throwing component content unmounted', !bHtml.includes('data-testid="ok"'), '');
__UNMOUNT_BOUNDARY__();

// ===== B) react-query cache + retry =====
__MOUNT_QUERY__();
await sleep(4000); // 2x 500 → 3 retries with backoff; leave enough time.
check('Auto-retry after failure and eventual success (ok:1)', document.querySelector('[data-testid="state"]')?.textContent === 'ok:1', document.querySelector('[data-testid="state"]')?.textContent);
check('Retry actually happened (backend hit >=3 times)', historyHits >= 3, 'hits=' + historyHits);

// Cache: re-issue a fetchQuery with the same key; within staleTime it should hit the cache and not hit the backend again.
const qc = window.__QC__;
const before = historyHits;
await qc.fetchQuery({ queryKey: ['robot-history', 'humanoid-042'], queryFn: () => Promise.reject(new Error('should not be called')) });
check('Cache hit within staleTime (no backend re-hit)', historyHits === before, `before=${before} after=${historyHits}`);

server.close();
let fail = 0;
for (const [, ok] of checks) if (!ok) fail++;
console.log(fail === 0 ? '\nOrder 26 all passed ✅' : `\nOrder 26 has failures ❌ (${fail})`);
process.exit(fail ? 1 : 0);
