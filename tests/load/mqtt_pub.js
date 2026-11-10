// Order 23 / I-16 —— decidable MQTT load test (k6 + xk6-mqtt).
//
// This file replaces emqtt-bench's "just emit numbers": emqtt-bench only reports
// "how many were sent", it can't judge "did the connection really succeed" / "what's the publish
// latency distribution" / "did WS receive". k6's check() turns every observation into PASS/FAIL
// that feeds directly into the exit code (R-07 §6.1 forbids "pass if no errors in logs").
//
// How to run (see tests/load/README.md for details)
// --------------------------------------------------
//   xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt
//   ./k6-mqtt run tests/load/mqtt_pub.js
//
// Note: what's needed is a **k6 binary with the xk6-mqtt extension**, not official k6.
//     Official k6 has no `k6/x/mqtt` module and errors at runtime with
//    "unknown module: k6/x/mqtt". The README gives three ways to obtain it.
//
// Empirical basis (don't write the API from memory)
// -------------------------------------------------
// - `import { Client } from "k6/x/mqtt"` —— xk6-mqtt mqtt/module.go:35
//   (`const ImportPath = "k6/x/mqtt"`) + examples/hello.js
// - `client.on(event, handler)`, event set connect/reconnect/end/error/message
//   —— mqtt/client_on.go:14-20 (`var events = map[string]struct{}{...}`)
// - `client.connect(url)`; **connect is synchronous**, returns already connected or already thrown
//   —— mqtt/client_connect.go:200-216 (`token.Wait()` blocks for CONNACK),
//      so `client.connected` is immediately readable after connect() (mqtt/client_connect.go:104-111)
// - `client.publish(topic, payload, {qos})` —— mqtt/client_publish.go:22
//   synchronous, internal `token.Wait()` (client_publish.go:76), i.e. returns when the packet is
//   written to the socket
// - `setInterval` / `setTimeout` available in the VU event loop —— README "Async Programming"
// - `client.end()` actively disconnects —— mqtt/client_end.go:14
//
// Relationship with mock_fleet.py
// -------------------------------
// **Do not replace mock_fleet.py** (explicit in Order 23). This script only handles "decidable
// load testing"; the payload structure is reused from tests/load/payload.js, and payload.js is
// locked field-by-field against mock_fleet.py by tests/load/test_payload_contract.py.

import { check, sleep } from 'k6';
import { Trend, Counter, Gauge, Rate } from 'k6/metrics';

import { Client } from 'k6/x/mqtt';

import { Robot, PUBLISH_INTERVAL_MS, topicFor } from './payload.js';
import {
  BROKER_URL,
  DURATION_S,
  EXPECTED_PUBLISHES,
  PUBLISH_HZ,
  RAMP_S,
  ROBOTS_PER_VU,
  ROBOT_COUNT,
  TARGET_MSG_PER_SEC,
  THRESHOLDS,
  VU_COUNT,
  buildOptions,
} from './config.js';

// ---------------------------------------------------------------- custom metrics
// These metrics make "decidable" quantities appear in k6's summary, not just k6's built-in http_*.
const pubLatency = new Trend('pub_latency_ms', true);
const pubTotal = new Counter('pub_total');
const pubErrors = new Counter('pub_errors');
const connectedVus = new Gauge('mqtt_connected_vus');
const connectOk = new Rate('mqtt_connect_ok');

// ---------------------------------------------------------------- VU-local state
// Each VU independently holds: its own Client, its own robot set, its own counters.
// Must not be global —— each k6 VU is an independent JS runtime; global semantics are easily misused.
let client = null;
let robots = [];
let publishTimer = null;
let stopTimer = null;
let tickIndex = 0;
let vuPublishes = 0;
let vuConnected = false;

/** The robot-index range this VU is responsible for (contiguous slice, for locating VU by robot_id). */
function myRobotIndices(vuId) {
  const start = (vuId - 1) * ROBOTS_PER_VU;
  const out = [];
  for (let i = 0; i < ROBOTS_PER_VU; i++) {
    const idx = start + i + 1; // robot_id starts from 001
    if (idx <= ROBOT_COUNT) out.push(idx);
  }
  return out;
}

/** Single publish: returns elapsed ms; on exception tally and return -1. */
function publishOnce(robot, tsMs) {
  const topic = topicFor(robot.robot_id);
  // mock_fleet.py:157 compacts JSON with separators=(',',':'); keep it consistent here,
  // otherwise the packet size differs from the real chain and throughput comparison is distorted.
  const payload = JSON.stringify(robot.buildPayload(tsMs));
  const t0 = Date.now();
  try {
    client.publish(topic, payload, { qos: 0 });
  } catch (err) {
    pubErrors.add(1);
    // Publish exceptions must explicitly fail the check, not silently continue.
    check(null, {
      'publish did not throw': () => false,
    });
    return -1;
  }
  const elapsed = Date.now() - t0;
  pubLatency.add(elapsed);
  pubTotal.add(1);
  vuPublishes += 1;
  return elapsed;
}

export const options = buildOptions();

export function setup() {
  // setup runs once, printing this run's "contract" —— so the log itself carries the judgment
  // basis, no need to guess where the thresholds came from afterward.
  console.log(
    `[load] broker=${BROKER_URL} robots=${ROBOT_COUNT} hz=${PUBLISH_HZ} ` +
      `vus=${VU_COUNT} duration=${DURATION_S}s ramp=${RAMP_S}s ` +
      `target=${TARGET_MSG_PER_SEC} msg/s expected=${EXPECTED_PUBLISHES} messages`
  );
  console.log(
    `[load] thresholds: connection success rate>=${THRESHOLDS.conn_success_rate} ` +
      `publish p95<${THRESHOLDS.pub_p95_ms}ms p99<${THRESHOLDS.pub_p99_ms}ms ` +
      `throughput ratio>=${THRESHOLDS.throughput_ratio}`
  );
  return { startedAt: Date.now() };
}

export default function (data) {
  const vuId = __VU;

  // ① Connect: turn "can't connect" into a decidable check, not silently running 0.
  if (client === null) {
    robots = myRobotIndices(vuId).map((i) => new Robot(i));
    client = new Client();

    // Event handlers: both error / end must leave evidence (README's events table,
    // implemented in mqtt/client_on.go:14-20). Register only once.
    client.on('error', (err) => {
      pubErrors.add(1);
      console.warn(`[load] VU ${vuId} mqtt error: ${err && err.message ? err.message : err}`);
    });
    client.on('end', () => {
      vuConnected = false;
      connectedVus.add(-1);
    });

    let connectError = null;
    try {
      client.connect(BROKER_URL);
    } catch (err) {
      connectError = err;
    }

    // connect is synchronous (client_connect.go:200-216 token.Wait()),
    // so reading `client.connected` here is the truth of "just finished connecting".
    const ok = client.connected === true && connectError === null;
    connectOk.add(ok);
    check({ connectError: connectError, connected: client.connected }, {
      'MQTT connection established': (v) => v.connected === true && v.connectError === null,
    });

    if (!ok) {
      console.error(
        `[load] VU ${vuId} connection failed: ${connectError ? connectError.message : 'client.connected=false'}`
      );
      // If connection fails, don't publish: let the check failure rate decide the exit code.
      // No sleep-retry here —— retrying would mask "broker not ready" as "slow".
      sleep(1);
      return;
    }

    vuConnected = true;
    connectedVus.add(1);

    // ② Fixed 10Hz publish rhythm. Use setInterval (VU event loop, README "Async Programming"),
    //    not `for + sleep` —— sleep would block the VU in the main function, unable to respond to end.
    publishTimer = setInterval(() => {
      if (!vuConnected || !client.connected) return;
      const tsMs = Date.now();
      for (const robot of robots) {
        robot.step(PUBLISH_INTERVAL_MS / 1000);
        publishOnce(robot, tsMs);
      }
      tickIndex += 1;
    }, PUBLISH_INTERVAL_MS);

    // ③ Settle at the end: clear timer → verify "publish rhythm meets expectation" → actively disconnect.
    stopTimer = setTimeout(() => {
      if (publishTimer !== null) {
        clearInterval(publishTimer);
        publishTimer = null;
      }

      // Expected publishes per VU in the steady-state window: per robot DURATION_S seconds × PUBLISH_HZ.
      const expectedPerVu = robots.length * PUBLISH_HZ * DURATION_S;
      // 20% tolerance: losing a few beats at ramp/settle is normal; beyond that means the timer was overwhelmed.
      const low = Math.floor(expectedPerVu * 0.8);
      check({ published: vuPublishes, expected: expectedPerVu, low: low }, {
        'VU published at least 80% of expected': (v) => v.published >= v.low,
      });

      if (client !== null) {
        client.end();
        client = null;
      }
      if (stopTimer !== null) {
        clearTimeout(stopTimer);
        stopTimer = null;
      }
    }, (DURATION_S + 1) * 1000);
  }

  // Main function can idle: the real publish rhythm is inside setInterval.
  // The sleep duration here decides whether the VU keeps looping before the scenario ends — 1s suffices.
  sleep(1);
}

export function teardown(data) {
  const elapsedS = (Date.now() - data.startedAt) / 1000;
  console.log(`[load] scenario ended, actual elapsed ${elapsedS.toFixed(1)}s`);
}

/**
 * handleSummary: write the "decidable result" to a file for CI / manual inspection.
 * Does not override k6's default stdout summary (the `stdout` in the returned object).
 */
export function handleSummary(data) {
  const checks = data.metrics.checks ? data.metrics.checks.values : {};
  const rate = checks.rate !== undefined ? checks.rate : 0;
  const pass = checks.passes !== undefined ? checks.passes : 0;
  const fail = checks.fails !== undefined ? checks.fails : 0;

  const sent = data.metrics.mqtt_messages_sent
    ? data.metrics.mqtt_messages_sent.values.count
    : 0;
  const pubLat = data.metrics.pub_latency_ms ? data.metrics.pub_latency_ms.values : {};

  const verdict = {
    generated_at: new Date().toISOString(),
    contract: {
      broker: BROKER_URL,
      robots: ROBOT_COUNT,
      publish_hz: PUBLISH_HZ,
      target_msg_per_sec: TARGET_MSG_PER_SEC,
      expected_publishes: EXPECTED_PUBLISHES,
      duration_s: DURATION_S,
      vu_count: VU_COUNT,
    },
    checks: { rate: rate, passes: pass, fails: fail, total: pass + fail },
    mqtt_messages_sent: sent,
    pub_latency_ms: {
      avg: pubLat.avg,
      p95: pubLat['p(95)'],
      p99: pubLat['p(99)'],
      max: pubLat.max,
    },
    thresholds_satisfied: (function () {
      const t = data.metrics.checks;
      return t && t.thresholds ? t.thresholds : null;
    })(),
  };

  return {
    stdout: '\n[load] judgment summary (checks rate=' + rate.toFixed(4) +
      ', pass=' + pass + ' fail=' + fail + ')\n',
    'tests/load/last-run.json': JSON.stringify(verdict, null, 2) + '\n',
  };
}
