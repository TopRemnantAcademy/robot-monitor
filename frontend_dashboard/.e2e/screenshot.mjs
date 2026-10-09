// 真实浏览器截图：注入 mock WS（假后端），观察 100 卡墙 / 顶栏 KPI / 告警流 / Drawer
import puppeteer from 'puppeteer';
import { WebSocketServer } from 'ws';
import http from 'node:http';

const WS_PORT = 8300, API_PORT = 8301;

// --- 假后端：WS 推 snapshot+telemetry+alert ---
const wss = new WebSocketServer({ port: WS_PORT });
const JOINT_NAMES = [
  'left_shoulder_pitch','left_shoulder_roll','left_shoulder_yaw','left_elbow_pitch','left_wrist_pitch','left_wrist_roll',
  'right_shoulder_pitch','right_shoulder_roll','right_shoulder_yaw','right_elbow_pitch','right_wrist_pitch','right_wrist_roll',
  'left_hip_yaw','left_hip_roll','left_hip_pitch','left_knee_pitch','left_ankle_pitch','left_ankle_roll',
  'right_hip_yaw','right_hip_roll','right_hip_pitch','right_knee_pitch','right_ankle_pitch','right_ankle_roll',
];
function makeRobot(i) {
  const id = `humanoid-${String(i + 1).padStart(3, '0')}`;
  const base = 32 + (i * 7) % 50;                       // 覆盖绿/黄/红三温区
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
    { robot_id: 'humanoid-018', rule_id: 'motor_temp', severity: 3, message: '左肩俯仰关节温度 82.3°C 超过阈值 75°C', ts: Date.now() - 1000 },
    { robot_id: 'humanoid-022', rule_id: 'battery', severity: 2, message: '电量 12.4% 低于阈值 15%', ts: Date.now() - 4000 },
    { robot_id: 'humanoid-035', rule_id: 'joint_stuck', severity: 2, message: '左膝俯仰关节角度 3.2s 内变化 < 0.01rad，疑似卡死', ts: Date.now() - 9000 },
    { robot_id: 'humanoid-051', rule_id: 'offline', severity: 3, message: '离线 12.6s 超过阈值 10s', ts: Date.now() - 15000 },
    { robot_id: 'humanoid-067', rule_id: 'motor_temp', severity: 2, message: '右肘俯仰关节温度 71.8°C 超过阈值 70°C', ts: Date.now() - 22000 },
  ];
  alerts.forEach((a, i) => setTimeout(() => ws.send(JSON.stringify({ type: 'alert', data: a })), 600 + i * 350));
  ws.on('close', () => clearInterval(iv));
});

// --- 假后端 REST：/robots/{id}/history ---
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

// 注入 WS/REST 指向假后端（覆盖 import.meta.env 的默认值）
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
console.log('=== 顶栏/首屏文本 ===\n' + kpi);

const cardCount = await page.evaluate(() => {
  // 卡片是 grid 内的 button，含 "H-0xx"
  return [...document.querySelectorAll('button')].filter((b) => /^H-\d{3}/.test(b.innerText.trim())).length;
});
console.log('卡片数:', cardCount);
await page.screenshot({ path: '/tmp/shot-main.png' });

// 打开 Drawer：点第一张卡片
await page.evaluate(() => {
  const btn = [...document.querySelectorAll('button')].find((b) => /^H-\d{3}/.test(b.innerText.trim()));
  btn.click();
});
await new Promise((r) => setTimeout(r, 2500));
const drawerText = await page.evaluate(() => document.body.innerText);
console.log('=== Drawer 打开后含关键信息 ===');
for (const k of ['基本信息', '关节温度阵列', '最近 5 分钟', '10s 分辨率', '温度(°C)', '扭矩(N·m)', '角度(rad)']) {
  console.log(` ${drawerText.includes(k) ? '✅' : '❌'} ${k}`);
}
await page.screenshot({ path: '/tmp/shot-drawer.png' });

await browser.close();
wss.close();
console.log('screenshots: /tmp/shot-main.png /tmp/shot-drawer.png');
process.exit(0);
