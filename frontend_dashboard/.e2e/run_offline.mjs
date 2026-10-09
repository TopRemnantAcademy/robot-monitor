// 验证要求 6：100 台无数据时渲染离线占位卡（humanoid-001..100），不得空白
import puppeteer from 'puppeteer';
const browser = await puppeteer.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
// 阻断 WS，模拟「后端不可达」
await page.evaluateOnNewDocument(() => {
  class DeadWS { constructor() { setTimeout(() => this.onclose && this.onclose(), 50); } close() {} send() {} }
  DeadWS.OPEN = 1; DeadWS.CONNECTING = 0;
  Object.defineProperty(window, 'WebSocket', { value: DeadWS, configurable: true });
});
await page.goto('http://127.0.0.1:4173/', { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1200));

const info = await page.evaluate(() => {
  const cards = [...document.querySelectorAll('button')].filter((b) => /^H-\d{3}/.test(b.innerText.trim()));
  const ids = cards.map((b) => b.innerText.trim().split('\n')[0]);
  const body = document.body.innerText;
  return {
    cardCount: cards.length,
    first: ids[0], last: ids[ids.length - 1],
    allOffline: cards.every((c) => c.innerText.includes('离线')),
    offlineLabels: (body.match(/离线/g) || []).length,
    onlineRate: (body.match(/在线率\s*\n?([\d.]+%)/) || [])[1],
    wsDot: body.includes('WS 断开重连中'),
  };
});
console.log('卡片数        :', info.cardCount, '(期望 100)');
console.log('首/末 ID      :', info.first, '/', info.last, '(期望 H-001 / H-100)');
console.log('全部为离线    :', info.allOffline);
console.log('“离线”出现次数:', info.offlineLabels);
console.log('在线率        :', info.onlineRate, '(期望 0.0%)');
console.log('WS 状态点     :', info.wsDot ? '断开重连中 ✅' : '❌');
await page.screenshot({ path: '/tmp/shot-offline.png' });

const checks = [
  ['无数据时仍渲染 100 张卡', info.cardCount === 100, info.cardCount],
  ['ID 为 humanoid-001..100 本地生成', info.first === 'H-001' && info.last === 'H-100', `${info.first}/${info.last}`],
  ['无数据时为离线占位（不空白）', info.allOffline, ''],
  ['在线率 0.0%', info.onlineRate === '0.0%', info.onlineRate],
  ['WS 断开时状态点提示', info.wsDot, ''],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, ex !== '' ? `[${ex}]` : ''); if (!ok) fail++; }
await browser.close();
process.exit(fail ? 1 : 0);
