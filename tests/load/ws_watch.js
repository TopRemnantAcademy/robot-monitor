// WS 收包率 / 首帧快照 的可判定断言（k6 + 官方内置 k6/ws 模块，**零新增依赖**）。
//
// 为什么单独一个脚本，而不是塞进 mqtt_pub.js
// ------------------------------------------
// 1) xk6-mqtt 是第三方扩展；`k6/ws` 是 k6 **内置**模块。混在一起写，会让
//    「没有 xk6-mqtt 的机器」连 WS 那一半也跑不了。拆开以后 WS 探针在任何
//    官方 k6 上都能跑。
// 2) 断言对象不同：mqtt_pub.js 判「发布侧」（R-08 C1/C2），本脚本判「订阅侧」
//    （R-08 C4/C5）。
//
// ⚠️ 这里**不**断言「前端渲染 10 次/秒」（R-08 E4）—— 那是浏览器内行为，
//    只能在真实前端里测。本脚本测的是**服务端到客户端的投递**。
//
// 实证依据
// --------
// - WS 路径 `/ws/robots`，鉴权走 `?token=`（backend_ingestion/app/main.py:251）
// - 信封三分支 snapshot / telemetry / alert（spec/00 §5.2；main.py:283/302/298）
// - snapshot 的 data 形状 `{"robots": [...]}`（main.py:283）
// - telemetry 的 data 是**单台**遥测对象、字段为数字（main.py:302；
//   state_cache._decode 负责把 Redis Hash 的字符串还原成数字，
//   见 services/state_cache.py:63-68）
// - 鉴权失败以 close code 1008 拒绝（main.py:271-274，RFC 6455 §7.4.1）
//
// ⚠️ 关于「被拒 → 重连风暴」这一坑（本机实测，勿改回去）
// ------------------------------------------------------
// k6 的 `ws.connect()` 在**握手被拒（1008）时会自动重试**，且重试期间
// `socket.on('error')` / `on('close')` 会**反复触发**。若把 check() 写在
// error/close 里，失败数会随重试次数指数级膨胀（实测一次 2s 会话炸出 43647 条
// fail），summary 完全失去意义。
// 因此本脚本：
//   1) 所有「收尾断言」只在 **首次** close 时结账一次（`settled` 标志）；
//   2) 未带 token 时**直接判为「未验证」并提前退出**，不进入风暴 ——
//      因为「被鉴权拒绝」既不是连通性故障，也不是本压测的判定对象。

import { check } from 'k6';
import ws from 'k6/ws';

import { Counter, Rate, Trend } from 'k6/metrics';

import { DURATION_S, RAMP_S, THRESHOLDS, WS_TOKEN, WS_URL } from './config.js';

const wsMessages = new Counter('ws_messages');
const wsSnapshotRobots = new Trend('ws_snapshot_robots', true);
const wsTelemetryFieldsOk = new Rate('ws_telemetry_fields_ok');
const wsUp = new Rate('ws_up');

const SESSION_TIMEOUT_MS = (RAMP_S + DURATION_S + 2) * 1000;

export const options = {
  scenarios: {
    ws_probe: {
      executor: 'constant-vus',
      vus: 1,
      duration: (RAMP_S + DURATION_S + 2) + 's',
      tags: { scenario: 'ws_probe' },
    },
  },
  thresholds: {
    // ⚠️ 这一条是**必须**的，不是锦上添花。
    // 反向验证（本机实测）：只把 check() 写进脚本、不接 `checks` 阈值，
    // 则在「snapshot 只有 50 台 + 字段被字符串化」的坏服务端上，
    // k6 仍打印 `ws_probe ✓` 并 **以退出码 0 结束** —— 断言红了却判通过。
    // 接上之后，同一场景退出码变 99（k6 阈值失败码）。
    checks: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
    ws_up: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
    // R-08 C5：telemetry 字段必须是数字而非字符串。
    ws_telemetry_fields_ok: ['rate>=' + THRESHOLDS.ws_telemetry_fields_rate.toFixed(4)],
    // R-08 C4：snapshot 必须含全部机器人（默认 100 台）。
    ws_snapshot_robots: ['min>=' + THRESHOLDS.ws_snapshot_robots],
  },
};

/** 拼 WS URL，带 ?token=（main.py:251 的 query 约定）。 */
function authedUrl() {
  if (!WS_TOKEN) return WS_URL;
  return WS_URL + (WS_URL.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(WS_TOKEN);
}

/** R-08 C5 的字段类型判定：数字而非字符串。 */
function telemetryPayloadOk(data) {
  return (
    data !== null &&
    typeof data === 'object' &&
    typeof data.robot_id === 'string' &&
    typeof data.status_code === 'number' &&
    typeof data.battery_pct === 'number' &&
    typeof data.max_joint_temp === 'number'
  );
}

export function setup() {
  // ⚠️ 未配置 token 必须在 setup 里就中止（而不是在每个 VU 迭代里 return）：
  // 放在 default 里会随迭代重复执行，把日志刷满同样的报错（本机实测一次 5s
  // 会话打印数十条），判定信号被噪音淹没。setup 抛错 = k6 立刻以非 0 退出，
  // run.sh 据此把它归到「未验证」而不是「判定失败」。
  if (!WS_TOKEN) {
    throw new Error(
      'WS_TOKEN 未设置：订单 15 起 /ws/robots 要求 ?token=<JWT>（backend_ingestion/app/main.py:251），' +
        '缺失时服务端以 close 1008 拒绝（main.py:271-274）。这是**预期鉴权行为**，不是连通性故障。 ' +
        '取一个 JWT 后重跑：' +
        'curl -s -X POST localhost:8000/auth/jwt/login -d "username=<email>&password=<pwd>" ' +
        '然后 WS_TOKEN=<access_token> bash tests/load/run.sh --ws-only'
    );
  }
  return { startedAt: Date.now() };
}

export default function (_data) {
  const res = ws.connect(authedUrl(), {}, function (socket) {
    // 收尾只结账一次 —— 见文件头「被拒 → 重连风暴」的说明。
    let settled = false;
    let sawSnapshot = false;
    let telemetryCount = 0;

    socket.on('open', function () {
      wsUp.add(1);
      check(null, { 'WS 握手成功（status 101）': () => true });
    });

    socket.on('message', function (raw) {
      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch (err) {
        check(raw, { 'WS 帧是合法 JSON': () => false });
        return;
      }
      wsMessages.add(1);

      if (envelope.type === 'snapshot') {
        sawSnapshot = true;
        const robots = envelope.data && envelope.data.robots;
        const n = Array.isArray(robots) ? robots.length : 0;
        wsSnapshotRobots.add(n);
        // R-08 C4：首帧 snapshot 必须含全部机器人。用 >= 而非 ==（服务端
        // 可能带附属字段），少于期望即 FAIL。
        check({ n: n, expected: THRESHOLDS.ws_snapshot_robots }, {
          'snapshot 含全部机器人': (v) => v.n >= v.expected,
        });
      } else if (envelope.type === 'telemetry') {
        telemetryCount += 1;
        const ok = telemetryPayloadOk(envelope.data);
        wsTelemetryFieldsOk.add(ok);
        // R-08 C5：字段必须是数字，不能是被字符串化的 "1"。
        check(envelope.data, {
          'telemetry 字段为数字而非字符串': (d) => telemetryPayloadOk(d),
        });
      } else if (envelope.type === 'alert') {
        check(envelope.data, {
          'alert 帧含 rule_id': (d) => d !== null && typeof d === 'object' && typeof d.rule_id === 'string',
        });
      }
    });

    socket.on('error', function () {
      // 只记账，不 check —— 重连时本回调会反复触发（见文件头说明）。
      wsUp.add(0);
    });

    socket.on('close', function (code) {
      if (settled) return;
      settled = true;

      // 1008 = policy violation（RFC 6455 §7.4.1；main.py:271 的鉴权拒绝）。
      check({ code: code }, {
        'WS 不是因鉴权被拒（非 1008）': (v) => v.code !== 1008,
      });

      check({ sawSnapshot: sawSnapshot, telemetry: telemetryCount }, {
        '收到 snapshot 首帧': (v) => v.sawSnapshot === true,
        '收到 telemetry 增量': (v) => v.telemetry > 0,
      });
    });

    socket.setTimeout(function () {
      socket.close();
    }, SESSION_TIMEOUT_MS);
  });

  // 握手在连接层就失败（连不上 / 非 101）时，res 会是 undefined 或非 101。
  if (!res || res.status !== 101) {
    wsUp.add(0);
    check(res, { 'WS 升级到 101': (r) => r !== null && r !== undefined && r.status === 101 });
  }
}

export function handleSummary(data) {
  const robotsTrend = data.metrics.ws_snapshot_robots
    ? data.metrics.ws_snapshot_robots.values
    : {};
  const msgs = data.metrics.ws_messages ? data.metrics.ws_messages.values.count : 0;
  const checks = data.metrics.checks ? data.metrics.checks.values : {};
  const fieldsOk = data.metrics.ws_telemetry_fields_ok
    ? data.metrics.ws_telemetry_fields_ok.values.rate
    : 0;

  const rate = checks.rate !== undefined ? checks.rate : 0;
  const pass = checks.passes !== undefined ? checks.passes : 0;
  const failCount = checks.fails !== undefined ? checks.fails : 0;

  const verdict = {
    generated_at: new Date().toISOString(),
    ws_url_authed: WS_TOKEN ? true : false,
    checks: { rate: rate, passes: pass, fails: failCount },
    ws_messages: msgs,
    snapshot_robots_max: robotsTrend.max !== undefined ? robotsTrend.max : null,
    telemetry_fields_number_rate: fieldsOk,
    note:
      '本脚本只判「服务端→客户端投递」（R-08 C4/C5）。前端 100ms 节流' +
      '（R-08 E4 的 ~10 次/秒）需在浏览器内验证，见 tests/load/README.md 的「未验证」章节。',
  };

  return {
    stdout:
      '\n[ws] 判定摘要（checks rate=' + rate.toFixed(4) + '，pass=' +
      pass + ' fail=' + failCount + '，messages=' + msgs + '）\n',
    'tests/load/last-ws-run.json': JSON.stringify(verdict, null, 2) + '\n',
  };
}
