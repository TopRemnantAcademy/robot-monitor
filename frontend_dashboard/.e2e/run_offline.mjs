// Verify requirement 6: when there is no data for the 100 robots, render offline placeholder cards (humanoid-001..100); must not be blank.
import puppeteer from 'puppeteer';
const browser = await puppeteer.launch({ args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900 });
// Block WS to simulate "backend unreachable".
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
    allOffline: cards.every((c) => c.innerText.includes('Offline')),
    offlineLabels: (body.match(/Offline/g) || []).length,
    onlineRate: (body.match(/Online rate\s*\n?([\d.]+%)/) || [])[1],
    wsDot: body.includes('WS Reconnecting'),
  };
});
console.log('Card count      :', info.cardCount, '(expected 100)');
console.log('First/last ID   :', info.first, '/', info.last, '(expected H-001 / H-100)');
console.log('All offline     :', info.allOffline);
console.log('"Offline" count :', info.offlineLabels);
console.log('Online rate     :', info.onlineRate, '(expected 0.0%)');
console.log('WS status dot   :', info.wsDot ? 'Reconnecting ✅' : '❌');
await page.screenshot({ path: '/tmp/shot-offline.png' });

const checks = [
  ['Renders 100 cards even with no data', info.cardCount === 100, info.cardCount],
  ['IDs humanoid-001..100 generated locally', info.first === 'H-001' && info.last === 'H-100', `${info.first}/${info.last}`],
  ['Offline placeholder when no data (not blank)', info.allOffline, ''],
  ['Online rate 0.0%', info.onlineRate === '0.0%', info.onlineRate],
  ['WS status dot shown when disconnected', info.wsDot, ''],
];
let fail = 0;
for (const [n, ok, ex] of checks) { console.log(ok ? '  ✅' : '  ❌', n, ex !== '' ? `[${ex}]` : ''); if (!ok) fail++; }
await browser.close();
process.exit(fail ? 1 : 0);
