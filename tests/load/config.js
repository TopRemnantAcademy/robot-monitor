// 压测参数与「可判定断言」的集中定义。
//
// 设计原则：**断言必须能被机器判 PASS/FAIL**，不接受「只吐数字」。
// 全部阈值默认值都能用环境变量覆盖，方便在不同规模的机器上重跑，
// 但默认值本身要有依据 —— 依据写在每个阈值旁边（R-07/R-08 的哪一条）。

import { PUBLISH_INTERVAL_MS } from './payload.js';

const env = (name, fallback) => (__ENV[name] === undefined ? fallback : __ENV[name]);
const envInt = (name, fallback) => parseInt(env(name, String(fallback)), 10);
const envFloat = (name, fallback) => parseFloat(env(name, String(fallback)));

/** N 台机器人。R-08 B1 的验收规模是 100 台（spec/00 §1）。 */
export const ROBOT_COUNT = envInt('ROBOTS', 100);

/** 每台每秒 10 条（spec/00 §1：10Hz）。 */
export const PUBLISH_HZ = envInt('PUBLISH_HZ', 10);

/** 稳态压测时长（秒）。R-07 §2 的落库真值校验用 30s。 */
export const DURATION_S = envInt('DURATION_S', 30);

/** 爬坡时长（秒）：给 VU 依次建连的时间，不计入稳态统计。 */
export const RAMP_S = envInt('RAMP_S', 5);

/** MQTT broker 地址（xk6-mqtt 的 connect 接受 mqtt:// URL，见 index.d.ts:303）。 */
export const BROKER_URL = env('BROKER_URL', 'mqtt://localhost:1883');

/** 后端 WS 端点与可选 JWT（订单 15 起，WS 需要 `?token=`，见 main.py:251）。 */
export const WS_URL = env('WS_URL', 'ws://localhost:8000/ws/robots');
export const WS_TOKEN = env('WS_TOKEN', '');

/**
 * 每个 VU 承担多少台机器人的发布节奏。
 * 1 个 VU 用 setInterval 串行 publish —— 与 mock_fleet.py 的单 publisher 设计同源
 * （mock_fleet.py:150-166 注释：多协程并发写 aiomqtt 会堆积 pending publish）。
 */
export const ROBOTS_PER_VU = envInt('ROBOTS_PER_VU', 1);

/**
 * VU 数 = ceil(机器人台数 / 每 VU 台数)。
 * ⚠️ 这台机器的 k6 是单进程多 goroutine，**不等价于 mock_fleet 的单进程**：
 * 100 个 VU 就有 100 个 MQTT 连接。EMQX 的 max_connections 默认远大于此，
 * 但若把 ROBOTS 调到上千，先确认 broker 的连接数与 `emqx ctl` 指标。
 */
export const VU_COUNT = Math.max(1, Math.ceil(ROBOT_COUNT / ROBOTS_PER_VU));

// ---------------------------------------------------------------- 可判定阈值

/**
 * 判定阈值。键名与 `check()` 里的断言一一对应，改这里就是改判定标准。
 * 依据来源逐条标注，禁止无据放宽（R-07 §6.2「禁止为通过而放宽阈值」）。
 */
export const THRESHOLDS = {
  // ① 连接成功率：R-07 §1 要求「就绪判据是真实探测，禁止裸 sleep」。
  //    连接建不起来 = 环境未就绪，必须显式 FAIL 而不是把 0 条当成「跑得快」。
  //    99% 而非 100%：允许个别 VU 在爬坡期撞上 broker 的瞬时 connect 限流。
  conn_success_rate: envFloat('TH_CONN_SUCCESS_RATE', 0.99),

  // ② 发布延迟：qos=0 下 publish() 是「写入 socket」的同步代价。
  //    R-07 §3 的 P95 < 200ms 是**落库批耗时**，不是本项；本项是发布侧观测，
  //    阈值取 50ms 量级（单条 publish 在本地 broker 上通常 < 1ms，留足抖动余量）。
  pub_p95_ms: envFloat('TH_PUB_P95_MS', 50.0),
  pub_p99_ms: envFloat('TH_PUB_P99_MS', 200.0),

  // ③ WS 收包率：R-08 E4 的判据是「1000 msg/s 进浏览器、渲染 ≈10 次/秒」。
  //    这里测的是**服务端到客户端的投递率**，不测前端渲染（渲染由前端单测覆盖）。
  //    R-08 C4/C5 要求 snapshot 含 100 台、telemetry 字段是数字。
  ws_snapshot_robots: envInt('TH_WS_SNAPSHOT_ROBOTS', ROBOT_COUNT),
  /** R-08 C5：telemetry 字段为数字的比率。允许 1% 抖动。 */
  ws_telemetry_fields_rate: envFloat('TH_WS_TELEMETRY_FIELDS_RATE', 0.99),
  ws_delivery_rate: envFloat('TH_WS_DELIVERY_RATE', 0.90),

  // ④ 吞吐：R-07 §3「端到端吞吐 ≥ 1000 msg/s」。容差取目标值的 95%。
  throughput_ratio: envFloat('TH_THROUGHPUT_RATIO', 0.95),
};

/** 目标吞吐（msg/s），用于打印与吞吐判定。 */
export const TARGET_MSG_PER_SEC = ROBOT_COUNT * PUBLISH_HZ;

/** 稳态阶段期望发布总条数。 */
export const EXPECTED_PUBLISHES = TARGET_MSG_PER_SEC * DURATION_S;

/**
 * k6 的 options（含 scenarios 与 thresholds）。
 *
 * scenarios 用 ramping-vus 而非 constant-vus：
 * 爬坡期让 VU 依次建连，避免 100 个 TCP 同时握手把 broker 的 SYN 队列打满，
 * 从而把「连接风暴」误读成「连接成功率低」。
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
    // k6 原生 thresholds：把「check 失败率」「自定义 Trend 分位」直接接到退出码上。
    // 与 check() 是两层保险 —— check() 给逐条证据，thresholds 给 CI 的 0/1 判定。
    thresholds: {
      checks: ['rate>=' + THRESHOLDS.conn_success_rate.toFixed(4)],
      pub_latency_ms: [
        'p(95)<' + THRESHOLDS.pub_p95_ms,
        'p(99)<' + THRESHOLDS.pub_p99_ms,
      ],
      mqtt_messages_sent: ['count>0'],
    },
    // 每个 VU 独占一个 MQTT 连接，不共享（xk6-mqtt 的 Client 绑定 VU 事件循环）。
    noConnectionReuse: false,
    // 关掉 k6 的默认 http 指标噪音（本脚本不发 HTTP；WS 走独立探针）。
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  };
}
