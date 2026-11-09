import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';

const OUT = path.resolve('docs/assets');
fs.mkdirSync(OUT, { recursive: true });
const BASE = process.env.CAPTURE_BASE ?? 'http://127.0.0.1:5173';

const browser = await puppeteer.launch({
  headless: 'shell',
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1.5 });
await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });

await page.waitForSelector('input[type="email"]', { timeout: 20000 });
await page.type('input[type="email"]', 'dev@example.com');
await page.type('input[type="password"]', 'devpass');
await page.click('form button[type="submit"]');
await page.waitForFunction(() => /H-\d{3}/.test(document.body.innerText), { timeout: 30000 });
await new Promise((r) => setTimeout(r, 3500));

const clicked = await page.evaluate(() => {
  const btn = [...document.querySelectorAll('button')].find((b) => /^H-\d{3}/.test((b.textContent || '').trim()));
  if (btn) { btn.click(); return (btn.textContent || '').trim(); }
  return false;
});
console.log('clicked card:', clicked);
await new Promise((r) => setTimeout(r, 2200));
await page.screenshot({ path: path.join(OUT, 'robot-detail-drawer.png') });
console.log('captured robot-detail-drawer.png');
await browser.close();
