// Real-browser screenshot: inject a mock WS (fake backend) and observe the 100-card wall / top-bar KPIs / alert stream / Drawer.
import puppeteer from 'puppeteer';
import { WebSocketServer } from 'ws';
import http from 'node:http';

const WS_PORT = 8300, API_PORT = 8301;

// --- Fake backend: WS pushes snapshot+telemetry+alert ---
const wss = new WebSocketServer({ port: WS_PORT });
const JOINT_NAMES = [
  'left_shoulder_pitch','left_shoulder_roll','left_shoulder_yaw','left_elbow_pitch','left_wrist_pitch','left_wrist_roll',
  'right_shoulder_pitch','right_shoulder_roll','right_shoulder_yaw','right_elbow_pitch','right_wrist_pitch','right_wrist_roll',
  'left_hip_yaw','left_hip_roll','left_hip_pitch','left_knee_pitch','left_ankle_pitch','left_ankle_roll',
  'right_hip_yaw','right_hip_roll','right_hip_pitch','right_knee_pitch','right_ankle_pitch','right_ankle_roll',
];
function makeRobot(i) {
  const id = `humanoid-${String(i + 1).padStart(3, '0')}`;
  const base = 32 + (i * 7) % 50;                       // Cover the green/yellow/red temperature bands.
  const jt = {}; JOINT_NAMES.forEach((j, k) => { jt[j] = +(base + (k % 5) * 2.5).toFixed(1); });
  const status = i % 17 === 0 ? 3 : i % 7 === 0 ? 2 : i % 11 === 0 ? 0 : 1;
  return {
    robot_id: id, status_code: status,
    battery_pct: +(100 - (i * 3.1) % 100).toFixed(1),
    max_joint_temp: Math.max(...Object.values(jt)),
    joint_temperatures: jt,
    joint_torques: Object.fromEntries(JOINT_NAMES.map((j, k) => [j, +(1 + k * 0.08).toFixed(2)])),
    joint_positions: Object.fromEntries(JOINT_NAMES.map((j, k) => [j, +(k * 0.012).toFixed(3)])),
    error_code: status === 3 ? 1013 : 0, ts: Date.now(),
  };
}
const snapshot = Array.from({ length: 100 }, (_, i) => makeRobot(i));
wss.on('connection', (ws) => {
  ws.send(JSON.stringify({ type: 'snapshot', data: { robots: snapshot } }));
  const iv = setInterval(() => {
    for (let i = 0; i < 100; i++) {
      const r = makeRobot(i);
      r.joint_temperatures.left_shoulder_pitch = +(60 + (Date.now() / 100) % 25).toFixed(1);
      r.max_joint_temp = Math.max(...Object.values(r.joint_temperatures));
      ws.send(JSON.stringify({ type: 'telemetry', data: r }));
    }
  }, 100);
  const alerts = [
    { robot_id: 'humanoid-018', rule_id: 'motor_temp', severity: 3, message: 'Left shoulder pitch joint temp 82.3°C exceeds threshold 75°C', ts: Date.now() - 1000 },
    { robot_id: 'humanoid-022', rule_id: 'battery', severity: 2, message: 'Battery 12.4% below threshold 15%', ts: Date.now() - 4000 },
    { robot_id: 'humanoid-035', rule_id: 'joint_stuck', severity: 2, message: 'Left knee pitch joint angle changed < 0.01rad within 3.2s, suspected stuck', ts: Date.now() - 9000 },
    { robot_id: 'humanoid-051', rule_id: 'offline', severity: 3, message: 'Offline for 12.6s, exceeds threshold 10s', ts: Date.now() - 15000 },
    { robot_id: 'humanoid-067', rule_id: 'motor_temp', severity: 2, message: 'Right elbow pitch joint temp 71.8°C exceeds threshold 70°C', ts: Date.now() - 22000 },
  ];
  alerts.forEach((a, i) => setTimeout(() => ws.send(JSON.stringify({ type: 'alert', data: a })), 600 + i * 350));
  ws.on('close', () => clearInterval(iv));
});

// --- Fake backend REST: /robots/{id}/history ---
http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Content-Type', 'application/json');
  const m = req.url.match(/^\/robots\/([^/]+)\/history/);
  if (m) {
    const points = Array.from({ length: 30 }, (_, i) => {
      const jt = {}; JOINT_NAMES.forEach((j, k) => { jt[j] = +(45 + i * 0.8 + (k % 4)).toFixed(1); });
      return {
        time: new Date(Date.now() - (29 - i) * 10_000).toISOString(),
        battery_pct: +(30 + i * 0.5).toFixed(1),
        max_joint_temp: +(52 + i * 0.9).toFixed(1),
        joint_temperatures: jt,
        joint_torques: Object.fromEntries(JOINT_NAMES.map((j, k) => [j, +(2 + i * 0.05 + k * 0.02).toFixed(2)])),
        joint_positions: Object.fromEntries(JOINT_NAMES.map((j, k) => [j, +(i * 0.01 + k * 0.005).toFixed(3)])),
      };
    });
    res.end(JSON.stringify({ robot_id: m[1], points }));
  } else { res.statusCode = 404; res.end('{}'); }
}).listen(API_PORT);

const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-setuid-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 900, deviceScaleFactor: 1 });
page.on('console', (m) => { if (m.type() === 'error') console.log('[page error]', m.text()); });
page.on('pageerror', (e) => console.log('[pageerror]', e.message));

// Inject WS/REST to point at the fake backend (overriding import.meta.env defaults).
await page.evaluateOnNewDocument((wsPort, apiPort) => {
  const OrigWS = window.WebSocket;
  class PatchedWS extends OrigWS {
    constructor(url, protocols) {
      super(String(url).replace(/localhost:\d+/, `localhost:${wsPort}`), protocols);
    }
  }
  window.WebSocket = PatchedWS;
  const origFetch = window.fetch;
  window.fetch = (input, init) => {
    const u = typeof input === 'string' ? input : input.url;
    return origFetch(String(u).replace(/localhost:\d+/, `localhost:${apiPort}`), init);
  };
}, WS_PORT, API_PORT);

await page.goto('http://127.0.0.1:4173/', { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 3000));

const kpi = await page.evaluate(() => document.body.innerText.slice(0, 400));
console.log('=== Top bar / first-paint text ===\n' + kpi);

const cardCount = await page.evaluate(() => {
  // A card is a button inside the grid, containing an "H-0xx" label
  return [...document.querySelectorAll('button')].filter((b) => /^H-\d{3}/.test(b.innerText.trim())).length;
});
console.log('Card count:', cardCount);
await page.screenshot({ path: '/tmp/shot-main.png' });

// Open the Drawer: click the first card.
await page.evaluate(() => {
  const btn = [...document.querySelectorAll('button')].find((b) => /^H-\d{3}/.test(b.innerText.trim()));
  btn.click();
});
await new Promise((r) => setTimeout(r, 2500));
const drawerText = await page.evaluate(() => document.body.innerText);
console.log('=== Drawer open: contains key info ===');
for (const k of ['Basic info', 'Joint temperature array (24 joints, telemetry schema)', 'Last 5 min', '10s resolution', 'Temp (°C)', 'Torque (N·m)', 'Angle (rad)']) {
  console.log(` ${drawerText.includes(k) ? '✅' : '❌'} ${k}`);
}
await page.screenshot({ path: '/tmp/shot-drawer.png' });

await browser.close();
wss.close();
console.log('screenshots: /tmp/shot-main.png /tmp/shot-drawer.png');
process.exit(0);
