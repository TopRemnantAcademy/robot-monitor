// Dev-only: capture dashboard preview screenshots against the mock backend.
// Usage (from frontend_dashboard/):
//   1) node mock_backend.mjs                      (terminal A, :8787)
//   2) VITE_API_BASE=http://localhost:8787 VITE_WS_HOST=localhost \
//      VITE_WS_PORT=8787 npx vite --port 5173     (terminal B, :5173)
//   3) node scripts/capture-preview.mjs           (terminal C)
import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Resolve to the repo-root docs/assets (the location referenced by README),
// independent of the cwd the script is launched from. (__dirname is not available
// in ESM, so derive it from import.meta.url.)
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(SCRIPT_DIR, '../../docs/assets');
fs.mkdirSync(OUT, { recursive: true });
const BASE = process.env.CAPTURE_BASE ?? 'http://127.0.0.1:5173';

const browser = await puppeteer.launch({
  headless: 'shell',
  args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1.5 });

const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));

console.log('navigating', BASE);
await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });

// Login (mock backend accepts any credentials)
await page.waitForSelector('input[type="email"]', { timeout: 20000 });
await page.type('input[type="email"]', 'dev@example.com');
await page.type('input[type="password"]', 'devpass');
await page.click('form button[type="submit"]');

// Wait for the fleet to render (robot ids appear via WS)
await page.waitForFunction(
  () => /humanoid-\d{3}/.test(document.body.innerText),
  { timeout: 30000 },
);
await new Promise((r) => setTimeout(r, 3500)); // let WS stream + charts settle

await page.screenshot({ path: path.join(OUT, 'dashboard-preview.png') });
console.log('captured dashboard-preview.png');

// Try to open a robot detail drawer (click the element showing humanoid-001)
const opened = await page.evaluate(() => {
  const els = [...document.querySelectorAll('*')].filter(
    (e) => /humanoid-001/.test(e.textContent || '') && e.children.length < 8,
  );
  const target = els.sort((a, b) => a.textContent.length - b.textContent.length)[0];
  if (target) { target.click(); return true; }
  return false;
});
if (opened) {
  await new Promise((r) => setTimeout(r, 1800));
  await page.screenshot({ path: path.join(OUT, 'robot-detail-drawer.png') });
  console.log('captured robot-detail-drawer.png');
} else {
  console.log('drawer: no clickable robot element found, skipped');
}

console.log('PAGE ERRORS:', errors.length ? errors.slice(0, 8) : 'none');
await browser.close();
