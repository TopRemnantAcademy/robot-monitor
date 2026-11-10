// Central definition of load parameters and "decidable assertions".
//
// Design principle: **assertions must be machine-judgable as PASS/FAIL**; "just emit numbers" is
// rejected. All threshold defaults can be overridden via env vars, so the suite can be re-run at
// different scales, but the default values themselves must have a basis — written next to each
// threshold (which R-07/R-08 item).

import { PUBLISH_INTERVAL_MS } from './payload.js';

const env = (name, fallback) => (__ENV[name] === undefined ? fallback : __ENV[name]);
const envInt = (name, fallback) => parseInt(env(name, String(fallback)), 10);
const envFloat = (name, fallback) => parseFloat(env(name, String(fallback)));

/** Number of robots. R-08 B1 acceptance scale is 100 units (spec/00 §1). */
export const ROBOT_COUNT = envInt('ROBOTS', 100);

/** 10 messages per unit per second (spec/00 §1: 10Hz). */
export const PUBLISH_HZ = envInt('PUBLISH_HZ', 10);

/** Steady-state load duration (seconds). R-07 §2 uses 30s for the landing truth check. */
export const DURATION_S = envInt('DURATION_S', 30);

/** Ramp-up duration (seconds): time for VUs to connect sequentially, not counted in steady-state stats. */
export const RAMP_S = envInt('RAMP_S', 5);

/** MQTT broker address (xk6-mqtt's connect accepts mqtt:// URL, see index.d.ts:303). */
export const BROKER_URL = env('BROKER_URL', 'mqtt://localhost:1883');

/** Backend WS endpoint and optional JWT (from Order 15, WS requires `?token=`, see main.py:251). */
export const WS_URL = env('WS_URL', 'ws://localhost:8000/ws/robots');
export const WS_TOKEN = env('WS_TOKEN', '');

/**
 * How many robots' publish rhythm each VU handles.
 * 1 VU uses setInterval to publish serially —— same origin as mock_fleet.py's single publisher
 * design (mock_fleet.py:150-166 comment: multiple coroutines writing aiomqtt concurrently piles up
 * pending publishes).
 */
export const ROBOTS_PER_VU = envInt('ROBOTS_PER_VU', 1);

/**
 * VU count = ceil(robot count / robots per VU).
 * Note: this machine's k6 is single-process multi-goroutine, **not equivalent to mock_fleet's
 * single process**: 100 VUs means 100 MQTT connections. EMQX's max_connections default is far above
 * this, but if you dial ROBOTS into the thousands, first confirm the broker's connection count and
 * the `emqx ctl` metrics.
 */
export const VU_COUNT = Math.max(1, Math.ceil(ROBOT_COUNT / ROBOTS_PER_VU));

// ---------------------------------------------------------------- decidable thresholds

/**
 * Judgment thresholds. Key names map one-to-one to the assertions in `check()`; changing here
 * changes the judgment standard. Source cited per item; forbid loosening without basis
 * (R-07 §6.2 "forbid loosening thresholds just to pass").
 */
export const THRESHOLDS = {
  // ① Connection success rate: R-07 §1 requires "readiness criterion is a real probe, no bare
  //    sleep". Can't connect = environment not ready, must explicitly FAIL rather than treat 0
  //    as "runs fast". 99% not 100%: allow an occasional VU to hit the broker's transient connect
  //    rate-limit during ramp.
  conn_success_rate: envFloat('TH_CONN_SUCCESS_RATE', 0.99),

  // ② Publish latency: under qos=0, publish() is the synchronous cost of "writing to socket".
  //    R-07 §3's P95 < 200ms is the **batch landing cost**, not this item; this item is the
  //    publish-side observation, threshold on the order of 50ms (a single publish on a local
  //    broker is usually < 1ms, leaving ample jitter margin).
  pub_p95_ms: envFloat('TH_PUB_P95_MS', 50.0),
  pub_p99_ms: envFloat('TH_PUB_P99_MS', 200.0),

  // ③ WS receive rate: R-08 E4's criterion is "1000 msg/s into the browser, renders ≈10 times/sec".
  //    Here we test the **server-to-client delivery rate**, not frontend rendering (rendering is
  //    covered by frontend unit tests). R-08 C4/C5 require snapshot to contain 100 units and
  //    telemetry fields to be numeric.
  ws_snapshot_robots: envInt('TH_WS_SNAPSHOT_ROBOTS', ROBOT_COUNT),
  /** R-08 C5: ratio of telemetry fields that are numbers. Allow 1% jitter. */
  ws_telemetry_fields_rate: envFloat('TH_WS_TELEMETRY_FIELDS_RATE', 0.99),
  ws_delivery_rate: envFloat('TH_WS_DELIVERY_RATE', 0.90),

  // ④ Throughput: R-07 §3 "end-to-end throughput ≥ 1000 msg/s". Tolerance is 95% of the target.
  throughput_ratio: envFloat('TH_THROUGHPUT_RATIO', 0.95),
};

/** Target throughput (msg/s), for printing and throughput judgment. */
export const TARGET_MSG_PER_SEC = ROBOT_COUNT * PUBLISH_HZ;

/** Expected total publishes during the steady-state phase. */
export const EXPECTED_PUBLISHES = TARGET_MSG_PER_SEC * DURATION_S;

/**
 * k6's options (scenarios + thresholds).
 *
 * scenarios use ramping-vus rather than constant-vus:
 * the ramp lets VUs connect sequentially, avoiding 100 simultaneous TCP handshakes filling the
 * broker's SYN queue, which would misread a "connection storm" as "low connection success rate".
 */
export function buildOptions() {
  return {
    discardResponseBodies: false,
    scenarios: {
      mqtt_telemetry: {
        executor: 'ramping-vus',
        startVUs: 0,
        stages: [
          { duration: RAMP_S + 's', target: VU_COUNT },
          { duration: DURATION_S + 's', target: VU_COUNT },
          { duration: '2s', target: 0 },
        ],
        gracefulRampDown: '5s',
        tags: { scenario: 'mqtt_telemetry' },
      },
    },
    // Native k6 thresholds: hook "check failure rate" and "custom Trend quantiles" directly to the
    // exit code. Two layers of insurance with check() —— check() gives per-item evidence, thresholds
    // give CI's 0/1 judgment.
    thresholds: {
      checks: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
      pub_latency_ms: [
        'p(95)<' + THRESHOLDS.pub_p95_ms,
        'p(99)<' + THRESHOLDS.pub_p99_ms,
      ],
      mqtt_messages_sent: ['count>0'],
    },
    // Each VU owns one MQTT connection, not shared (xk6-mqtt's Client binds the VU event loop).
    noConnectionReuse: false,
    // Turn off k6's default http metric noise (this script sends no HTTP; WS has its own probe).
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  };
}
