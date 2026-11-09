// RobotMonitor 개발용 목(mock) 백엔드 — 실제 Docker 스택 없이 프론트엔드를 띄우기 위함.
//
// 목적: sandbox 등에서 Docker/TimescaleDB/EMQX/Redis 를 못 돌릴 때, 브라우저의
//       fetch(REST) + WebSocket 을 받아줄 경량 서버. 비밀번호 검증 없음(개발용).
//
// 사용:
//   node mock_backend.mjs                # 기본 8787
//   MOCK_PORT=8787 node mock_backend.mjs
//
// 프론트엔드 연결: Vite 를 아래 env 로 기동
//   VITE_API_BASE=http://localhost:8787 VITE_WS_HOST=localhost VITE_WS_PORT=8787 npm run dev
//
// 실제 백엔드(Docker)를 쓸 때는 이 목을 끄고 Docker 스택을 기동하면 됨.

import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';

const PORT = Number(process.env.MOCK_PORT ?? 8787);
const JOINT_NAMES = [
  'left_shoulder_pitch', 'left_shoulder_roll', 'left_shoulder_yaw', 'left_elbow_pitch',
  'left_wrist_pitch', 'left_wrist_roll',
  'right_shoulder_pitch', 'right_shoulder_roll', 'right_shoulder_yaw', 'right_elbow_pitch',
  'right_wrist_pitch', 'right_wrist_roll',
  'left_hip_yaw', 'left_hip_roll', 'left_hip_pitch',
  'left_knee_pitch', 'left_ankle_pitch', 'left_ankle_roll',
  'right_hip_yaw', 'right_hip_roll', 'right_hip_pitch',
  'right_knee_pitch', 'right_ankle_pitch', 'right_ankle_roll',
];

// ---- 100대 로봇 기본 상태(스냅샷) ----
function buildRobots() {
  const robots = [];
  for (let i = 1; i <= 100; i++) {
    const id = `humanoid-${String(i).padStart(3, '0')}`;
    // 결정적 분포: 10대 중 1대는 오프라인(0), 나머지 정상/경고/위험 혼합
    const offline = i % 10 === 0;
    const fault = i % 17 === 0;
    const warn = i % 7 === 0;
    const status_code = offline ? 0 : fault ? 3 : warn ? 2 : 1;
    const is_online = !offline;
    const base = 45 + ((i * 7) % 30); // 45~74
    const joint = (mul) => {
      const d = {};
      for (const n of JOINT_NAMES) d[n] = +(base + ((n.length * i) % 10) * mul).toFixed(1);
      return d;
    };
    robots.push({
      robot_id: id,
      status_code,
      error_code: 0,
      battery_pct: offline ? 0 : 30 + ((i * 3) % 70),
      max_joint_temp: base + 5,
      joint_temperatures: joint(1),
      joint_positions: joint(0.5),
      joint_torques: joint(0.2),
      ts: Math.floor(Date.now() / 1000),
      is_online,
      last_seen: is_online ? Math.floor(Date.now() / 1000) : null,
    });
  }
  return robots;
}
const ROBOTS = buildRobots();

// ---- REST 응답 헬퍼 (CORS 포함) ----
function sendJson(res, status, body, origin) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': origin ?? '*',
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  });
  res.end(JSON.stringify(body));
}
function corsPreflight(res, origin) {
  res.writeHead(204, {
    'Access-Control-Allow-Origin': origin ?? '*',
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  });
  res.end();
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => (data += c));
    req.on('end', () => resolve(data));
  });
}

// ---- 알림 샘플 (AlertApiItem 형식) ----
function sampleAlerts() {
  const now = new Date().toISOString();
  return [
    {
      alert_id: 1, time: now, robot_id: 'humanoid-017', rule_id: 'motor_temp',
      severity: 3, message: '관절 모터 온도 임계 초과 (mock)', detail: { joint: 'left_elbow_pitch', temp: 88.2 },
      ack: false, ack_at: null, ack_by: null, escalated: true, suppressed: false, suppressed_by: null,
    },
    {
      alert_id: 2, time: now, robot_id: 'humanoid-034', rule_id: 'battery_low',
      severity: 2, message: '배터리 잔량 낮음 (mock)', detail: { battery_pct: 18.4 },
      ack: false, ack_at: null, ack_by: null, escalated: false, suppressed: false, suppressed_by: null,
    },
    {
      alert_id: 3, time: now, robot_id: 'humanoid-051', rule_id: 'offline',
      severity: 3, message: '로봇 오프라인 (mock)', detail: null,
      ack: true, ack_at: now, ack_by: 'piaoyhyh@gmail.com', escalated: false, suppressed: false, suppressed_by: null,
    },
  ];
}

const server = createServer(async (req, res) => {
  const origin = req.headers.origin;
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  if (req.method === 'OPTIONS') return corsPreflight(res, origin);

  // 인증
  if (path === '/auth/jwt/login' && req.method === 'POST') {
    const body = await readBody(req);
    const params = new URLSearchParams(body);
    const email = params.get('username') || params.get('email') || 'piaoyhyh@gmail.com';
    return sendJson(res, 200, { access_token: `mock-jwt.${Buffer.from(email).toString('base64url')}`, token_type: 'bearer' }, origin);
  }
  if (path === '/auth/jwt/logout' && req.method === 'POST') {
    return sendJson(res, 200, {}, origin);
  }
  if (path === '/users/me' && req.method === 'GET') {
    return sendJson(res, 200, {
      id: 1, email: 'piaoyhyh@gmail.com', is_active: true, is_verified: true, role: 'admin',
    }, origin);
  }

  // 지원 기종
  if (path === '/supported-robots' && req.method === 'GET') {
    return sendJson(res, 200, {
      models: [
        {
          model_type: 'HUMANOID-24DOF', display_name: 'Humanoid 24-DOF', form_factor: '双足人形',
          dof: 24, vendor: 'RobotMonitor Platform', description: '플랫폼 주력 24 자유도 인간형.',
          generic: false, total: 80, online: 72, firmware_versions: ['1.0.0'], stations: 10,
        },
        {
          model_type: 'AMR-S4', display_name: 'AMR Service-4', form_factor: '轮式服务机器人',
          dof: 0, vendor: 'RobotMonitor Platform', description: '4륜 서비스 AMR · 카페/식당 배송.',
          generic: false, total: 20, online: 18, firmware_versions: ['1.2.0'], stations: 4,
        },
      ],
      total_robots: 100,
    }, origin);
  }

  // 단일 로봇 히스토리
  const histMatch = path.match(/^\/robots\/([^/]+)\/history$/);
  if (histMatch && req.method === 'GET') {
    return sendJson(res, 200, {
      robot_id: decodeURIComponent(histMatch[1]),
      points: [
        { time: new Date(Date.now() - 20000).toISOString(), battery_pct: 82.1, max_joint_temp: 64.3,
          joint_temperatures: { left_elbow_pitch: 60.0 }, joint_torques: { left_elbow_pitch: 1.5 }, joint_positions: { left_elbow_pitch: 0.42 } },
        { time: new Date(Date.now() - 10000).toISOString(), battery_pct: 81.7, max_joint_temp: 65.1,
          joint_temperatures: { left_elbow_pitch: 62.5 }, joint_torques: { left_elbow_pitch: 1.7 }, joint_positions: { left_elbow_pitch: 0.55 } },
      ],
    }, origin);
  }

  // 알림 목록
  if (path === '/alerts' && req.method === 'GET') {
    const items = sampleAlerts();
    return sendJson(res, 200, { items, total: items.length, limit: 50, offset: 0 }, origin);
  }
  // 알림 ack/unack
  const ackMatch = path.match(/^\/alerts\/(\d+)\/(ack|unack)$/);
  if (ackMatch && req.method === 'POST') {
    const id = Number(ackMatch[1]);
    const ack = ackMatch[2] === 'ack';
    return sendJson(res, 200, {
      alert_id: id, ack, ack_at: ack ? new Date().toISOString() : null,
      ack_by: ack ? 'piaoyhyh@gmail.com' : null, changed: true,
    }, origin);
  }

  // 헬스
  if (path === '/healthz' && req.method === 'GET') {
    return sendJson(res, 200, { status: 'ok', mock: true }, origin);
  }

  sendJson(res, 404, { detail: `mock: no handler for ${req.method} ${path}` }, origin);
});

// ---- WebSocket: 스냅샷 + 텔레메트리 스트림 ----
const wss = new WebSocketServer({ server, path: '/ws/robots' });
wss.on('connection', (ws) => {
  // ① 초기 스냅샷
  ws.send(JSON.stringify({ type: 'snapshot', data: { robots: ROBOTS } }));
  // 초기 알림 1건
  ws.send(JSON.stringify({ type: 'alert', data: { robot_id: 'humanoid-017', rule_id: 'motor_temp', severity: 3, message: '관절 모터 온도 임계 초과 (mock)', ts: Math.floor(Date.now() / 1000) } }));

  // ② 0.5s 마다 100대 텔레메트리 증분
  const timer = setInterval(() => {
    const ts = Math.floor(Date.now() / 1000);
    for (const base of ROBOTS) {
      if (!base.is_online) continue;
      const jitter = (k) => +(k + (Math.random() * 2 - 1)).toFixed(1);
      const joint = (mul) => {
        const d = {};
        for (const n of JOINT_NAMES) d[n] = jitter(base.joint_temperatures[n] * mul * 0.1 + 50);
        return d;
      };
      const tel = {
        robot_id: base.robot_id,
        status_code: base.status_code,
        error_code: 0,
        battery_pct: Math.max(0, Math.min(100, jitter(base.battery_pct))),
        max_joint_temp: jitter(base.max_joint_temp),
        joint_temperatures: joint(1),
        joint_positions: joint(0.5),
        joint_torques: joint(0.2),
        ts,
        is_online: true,
        last_seen: ts,
      };
      if (ws.readyState === ws.OPEN) ws.send(JSON.stringify({ type: 'telemetry', data: tel }));
    }
  }, 500);

  ws.on('close', () => clearInterval(timer));
});

server.listen(PORT, () => {
  console.log(`[mock-backend] REST + WS listening on http://localhost:${PORT} (ws: /ws/robots)`);
});
