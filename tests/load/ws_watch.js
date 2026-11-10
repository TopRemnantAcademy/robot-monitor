// WS receive-rate / first-frame snapshot decidable assertions (k6 + the built-in k6/ws module,
// **zero new dependencies**).
//
// Why a separate script instead of being folded into mqtt_pub.js
// -------------------------------------------------------------
// 1) xk6-mqtt is a third-party extension; `k6/ws` is a k6 **built-in** module. Mixing them means
//    a machine without xk6-mqtt can't even run the WS half. Splitting out, the WS probe runs on any
//    official k6.
// 2) Different assertion objects: mqtt_pub.js judges the "publish side" (R-08 C1/C2), this script
//    judges the "subscribe side" (R-08 C4/C5).
//
// Note: this script does **not** assert "frontend renders 10 times/sec" (R-08 E4) — that is
// browser-internal behavior, only testable in a real frontend. This script tests **server-to-client
// delivery**.
//
// Empirical basis
// ---------------
// - WS path `/ws/robots`, auth via `?token=` (backend_ingestion/app/main.py:251)
// - Three-branch envelope: snapshot / telemetry / alert (spec/00 §5.2; main.py:283/302/298)
// - snapshot data shape `{"robots": [...]}` (main.py:283)
// - telemetry data is a **single-unit** telemetry object with numeric fields (main.py:302;
//   state_cache._decode restores Redis Hash strings to numbers, see services/state_cache.py:63-68)
// - auth failure rejected with close code 1008 (main.py:271-274, RFC 6455 §7.4.1)
//
// Note: the "rejected -> reconnect storm" pitfall (measured locally, do not revert)
// ----------------------------------------------------------------------------
// k6's `ws.connect()` auto-retries on **handshake rejection (1008)**, and during retries
// `socket.on('error')` / `on('close')` fire **repeatedly**. If check() is written inside
// error/close, the failure count balloons exponentially with retries (measured: one 2s session
// blew up to 43647 fails), making the summary meaningless.
// So this script:
//   1) all "final assertions" settle exactly once on the **first** close (`settled` flag);
//   2) when no token is present, it **directly judges "unverified" and exits early**, avoiding the
//      storm — because "auth rejected" is neither a connectivity failure nor this load test's object.

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
    // Note: this one is **mandatory**, not a nice-to-have.
    // Reverse-verified (measured locally): writing only check() without hooking the `checks`
    // threshold means that on a bad server ("snapshot only 50 units + fields stringified"),
    // k6 still prints `ws_probe ✓` and **ends with exit code 0** — assertion red but judged pass.
    // After hooking, the same scenario exits 99 (k6 threshold-failure code).
    checks: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
    ws_up: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
    // R-08 C5: telemetry fields must be numbers, not strings.
    ws_telemetry_fields_ok: ['rate>=' + THRESHOLDS.ws_telemetry_fields_rate.toFixed(4)],
    // R-08 C4: snapshot must contain all robots (default 100 units).
    ws_snapshot_robots: ['min>=' + THRESHOLDS.ws_snapshot_robots],
  },
};

/** Build the WS URL with ?token= (main.py:251 query convention). */
function authedUrl() {
  if (!WS_TOKEN) return WS_URL;
  return WS_URL + (WS_URL.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(WS_TOKEN);
}

/** R-08 C5 field-type judgment: numeric, not string. */
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
  // Note: missing token must abort in setup (not return inside each VU iteration):
  // putting it in default re-executes per iteration, flooding logs with the same error (measured:
  // dozens of lines in one 5s session), drowning the judgment signal. setup throwing = k6 exits
  // non-zero immediately; run.sh then classifies it as "unverified" instead of "judgment failed".
  if (!WS_TOKEN) {
    throw new Error(
      'WS_TOKEN not set: from Order 15, /ws/robots requires ?token=<JWT> (backend_ingestion/app/main.py:251); ' +
        'without it the server rejects with close 1008 (main.py:271-274). This is the **expected auth ' +
        'behavior**, not a connectivity failure. ' +
        'Get a JWT and rerun: ' +
        'curl -s -X POST localhost:8000/auth/jwt/login -d "username=<email>&password=<pwd>" ' +
        'then WS_TOKEN=<access_token> bash tests/load/run.sh --ws-only'
    );
  }
  return { startedAt: Date.now() };
}

export default function (_data) {
  const res = ws.connect(authedUrl(), {}, function (socket) {
    // Settle only once —— see the file-header "rejected -> reconnect storm" note.
    let settled = false;
    let sawSnapshot = false;
    let telemetryCount = 0;

    socket.on('open', function () {
      wsUp.add(1);
      check(null, { 'WS handshake succeeded (status 101)': () => true });
    });

    socket.on('message', function (raw) {
      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch (err) {
        check(raw, { 'WS frame is valid JSON': () => false });
        return;
      }
      wsMessages.add(1);

      if (envelope.type === 'snapshot') {
        sawSnapshot = true;
        const robots = envelope.data && envelope.data.robots;
        const n = Array.isArray(robots) ? robots.length : 0;
        wsSnapshotRobots.add(n);
        // R-08 C4: the first-frame snapshot must contain all robots. Use >= not == (server
        // may carry extra fields); fewer than expected fails.
        check({ n: n, expected: THRESHOLDS.ws_snapshot_robots }, {
          'snapshot contains all robots': (v) => v.n >= v.expected,
        });
      } else if (envelope.type === 'telemetry') {
        telemetryCount += 1;
        const ok = telemetryPayloadOk(envelope.data);
        wsTelemetryFieldsOk.add(ok);
        // R-08 C5: fields must be numbers, not stringified "1".
        check(envelope.data, {
          'telemetry fields are numbers not strings': (d) => telemetryPayloadOk(d),
        });
      } else if (envelope.type === 'alert') {
        check(envelope.data, {
          'alert frame contains rule_id': (d) => d !== null && typeof d === 'object' && typeof d.rule_id === 'string',
        });
      }
    });

    socket.on('error', function () {
      // Only tally, don't check —— this callback fires repeatedly on reconnect (see file-header note).
      wsUp.add(0);
    });

    socket.on('close', function (code) {
      if (settled) return;
      settled = true;

      // 1008 = policy violation (RFC 6455 §7.4.1; main.py:271 auth rejection).
      check({ code: code }, {
        'WS not rejected due to auth (non-1008)': (v) => v.code !== 1008,
      });

      check({ sawSnapshot: sawSnapshot, telemetry: telemetryCount }, {
        'received snapshot first frame': (v) => v.sawSnapshot === true,
        'received telemetry increments': (v) => v.telemetry > 0,
      });
    });

    socket.setTimeout(function () {
      socket.close();
    }, SESSION_TIMEOUT_MS);
  });

  // When the handshake fails at the connection layer (can't connect / not 101), res is undefined
  // or not 101.
  if (!res || res.status !== 101) {
    wsUp.add(0);
    check(res, { 'WS upgraded to 101': (r) => r !== null && r !== undefined && r.status === 101 });
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
      'This script only judges "server-to-client delivery" (R-08 C4/C5). The frontend 100ms ' +
      'throttle (R-08 E4, ~10 times/sec) must be verified inside the browser; see the ' +
      '"Unverified" section of tests/load/README.md.',
  };

  return {
    stdout:
      '\n[ws] judgment summary (checks rate=' + rate.toFixed(4) + ', pass=' +
      pass + ' fail=' + failCount + ', messages=' + msgs + ')\n',
    'tests/load/last-ws-run.json': JSON.stringify(verdict, null, 2) + '\n',
  };
}
