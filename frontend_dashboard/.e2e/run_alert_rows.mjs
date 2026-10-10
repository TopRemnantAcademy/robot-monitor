// 验证：新告警到达只挂载 1 行，不重建整表（AlertLogTable memo 生效）
//
// 反证方式：本用例在「告警行未 memo」的旧实现下必然失败 ——
// 每 push 1 条告警，整表 60 行都会因 diff 失败而被重建。
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
// 入口已挂载，这里只需等待首次提交完成
await new Promise((r) => setTimeout(r, 100));

const mk = (i, sev) => ({
  robot_id: `humanoid-${String(i % 100 + 1).padStart(3, '0')}`,
  rule_id: 'motor_temp', severity: sev,
  message: `关节温度过高 #${i}`, ts: Date.now() + i,
});

// ① 先预置 40 行（分多次 push，settle 后每行已 memo 稳定）
for (let i = 0; i < 40; i++) {
  ReactDOM.flushSync ? ReactDOM.flushSync(() => {}) : null;
  window.__ALERT__.push(mk(i, 2));
  await new Promise((r) => setTimeout(r, 5));
}
await new Promise((r) => setTimeout(r, 150));
const beforeRows = (root.textContent.match(/S\d/g) || []).length;

// ② 再 push 5 条新告警，统计新增 DOM 行数
window.__ALERT__.reset();
for (let i = 100; i < 105; i++) {
  window.__ALERT__.push(mk(i, 3));
  await new Promise((r) => setTimeout(r, 20));
}
await new Promise((r) => setTimeout(r, 200));

const created = window.__ALERT__.rowCreations;
const afterRows = (root.textContent.match(/S\d/g) || []).length;

console.log('预置告警行数      :', beforeRows, '(期望 40)');
console.log('push 5 条后行数   :', afterRows, '(期望 45)');
console.log('新增 DOM 行节点数 :', created, '(期望 ≈5，旧实现约 5×45=225)');

const checks = [
  ['前置告警确实渲染了 40 行', beforeRows === 40, beforeRows],
  ['新告警被追加渲染', afterRows === beforeRows + 5, `${beforeRows}→${afterRows}`],
  ['每条新告警只重建 1 行（memo 生效）', created > 0 && created <= 8, created],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, `[${ex}]`); if (!ok) fail++; }
process.exit(fail ? 1 : 0);
