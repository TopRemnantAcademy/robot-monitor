// Verify: when a new alert arrives, only 1 row is mounted; the whole table is not rebuilt (AlertLogTable memo working).
//
// Proof-by-contradiction: this case would inevitably fail under the old implementation where alert rows were not memoized ——
// each pushed alert would cause all 60 rows to be rebuilt due to a failed diff.
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(global, 'navigator', { value: dom.window.navigator, configurable: true });
global.DOMException = dom.window.DOMException;
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.MutationObserver = dom.window.MutationObserver;
global.MessageEvent = dom.window.MessageEvent;
global.Event = dom.window.Event;
global.IS_REACT_ACT_ENVIRONMENT = false;
global.IS_REACT_LEGACY_ENV = false;

const require = createRequire('file:///workspace/frontend_dashboard/index.js');
require('./.e2e/alert.cjs');
const ReactDOM = require('react-dom/client');
const React = require('react');

const root = document.getElementById('root');
// The entry is already mounted; here we only wait for the first commit to finish.
await new Promise((r) => setTimeout(r, 100));

const mk = (i, sev) => ({
  robot_id: `humanoid-${String(i % 100 + 1).padStart(3, '0')}`,
  rule_id: 'motor_temp', severity: sev,
  message: `Joint temp too high #${i}`, ts: Date.now() + i,
});

// ① First pre-seed 40 rows (pushed in batches; after settling each row's memo is stable).
for (let i = 0; i < 40; i++) {
  ReactDOM.flushSync ? ReactDOM.flushSync(() => {}) : null;
  window.__ALERT__.push(mk(i, 2));
  await new Promise((r) => setTimeout(r, 5));
}
await new Promise((r) => setTimeout(r, 150));
const beforeRows = (root.textContent.match(/S\d/g) || []).length;

// ② Then push 5 new alerts and count the newly added DOM rows.
window.__ALERT__.reset();
for (let i = 100; i < 105; i++) {
  window.__ALERT__.push(mk(i, 3));
  await new Promise((r) => setTimeout(r, 20));
}
await new Promise((r) => setTimeout(r, 200));

const created = window.__ALERT__.rowCreations;
const afterRows = (root.textContent.match(/S\d/g) || []).length;

console.log('Pre-seeded alert rows   :', beforeRows, '(expected 40)');
console.log('Rows after pushing 5    :', afterRows, '(expected 45)');
console.log('New DOM row nodes added :', created, '(expected ≈5; old impl ≈ 5×45=225)');

const checks = [
  ['Pre-seeded alerts really rendered 40 rows', beforeRows === 40, beforeRows],
  ['New alerts appended', afterRows === beforeRows + 5, `${beforeRows}→${afterRows}`],
  ['Each new alert rebuilds only 1 row (memo working)', created > 0 && created <= 8, created],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, `[${ex}]`); if (!ok) fail++; }
process.exit(fail ? 1 : 0);
