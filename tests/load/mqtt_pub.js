// 订单 23 / I-16 —— 可判定的 MQTT 压测（k6 + xk6-mqtt）。
//
// 这个文件替换的是 emqtt-bench 的「只吐数字」：emqtt-bench 只报告
// 「发了多少条」，判不了「连接是否真的成功」「发布延迟分布如何」「WS 是否收到」。
// k6 的 check() 让每条观测都变成 PASS/FAIL，直接进退出码（R-07 §6.1 禁止
// 「日志无报错即通过」）。
//
// 跑法（详见 tests/load/README.md）
// -------------------------------
//   xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt
//   ./k6-mqtt run tests/load/mqtt_pub.js
//
// ⚠️ 需要的是**带 xk6-mqtt 扩展的 k6 二进制**，不是官方 k6。
//    官方 k6 没有 `k6/x/mqtt` 模块，运行时会报
//    "unknown module: k6/x/mqtt"。README 给了三条获取途径。
//
// 实证依据（不许凭记忆写 API）
// ---------------------------
// - `import { Client } from "k6/x/mqtt"` —— xk6-mqtt mqtt/module.go:35
//   （`const ImportPath = "k6/x/mqtt"`）+ examples/hello.js
// - `client.on(event, handler)`，事件集合 connect/reconnect/end/error/message
//   —— mqtt/client_on.go:14-20（`var events = map[string]struct{}{...}`）
// - `client.connect(url)`；**connect 是同步的**，返回时已建连或已抛错
//   —— mqtt/client_connect.go:200-216（`token.Wait()` 阻塞等 CONNACK），
//      故 `client.connected` 在 connect() 之后立即可读（mqtt/client_connect.go:104-111）
// - `client.publish(topic, payload, {qos})` —— mqtt/client_publish.go:22
//   同步版内部 `token.Wait()`（client_publish.go:76），即返回时该包已写入 socket
// - `setInterval` / `setTimeout` 在 VU 事件循环里可用 —— README「Async Programming」
// - `client.end()` 主动断开 —— mqtt/client_end.go:14
//
// 与 mock_fleet.py 的关系
// -----------------------
// **不替换 mock_fleet.py**（订单 23 明确）。本脚本只负责「可判定的压测」，
// payload 结构从 tests/load/payload.js 复用，而 payload.js 由
// tests/load/test_payload_contract.py 与 mock_fleet.py 逐字段对拍锁死。

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

// ---------------------------------------------------------------- 自定义指标
// 这些指标让 k6 的 summary 里出现「可判定」的量，而不只是 k6 内建的 http_*。
const pubLatency = new Trend('pub_latency_ms', true);
const pubTotal = new Counter('pub_total');
const pubErrors = new Counter('pub_errors');
const connectedVus = new Gauge('mqtt_connected_vus');
const connectOk = new Rate('mqtt_connect_ok');

// ---------------------------------------------------------------- VU 局部状态
// 每个 VU 独立持有：自己的 Client、自己的机器人集合、自己的计数。
// 不能放 global —— k6 的每个 VU 是独立 JS 运行时，global 语义容易误用。
let client = null;
let robots = [];
let publishTimer = null;
let stopTimer = null;
let tickIndex = 0;
let vuPublishes = 0;
let vuConnected = false;

/** 本 VU 负责的机器人下标区间（连续切片，便于按 robot_id 定位 VU）。 */
function myRobotIndices(vuId) {
  const start = (vuId - 1) * ROBOTS_PER_VU;
  const out = [];
  for (let i = 0; i < ROBOTS_PER_VU; i++) {
    const idx = start + i + 1; // robot_id 从 001 起
    if (idx <= ROBOT_COUNT) out.push(idx);
  }
  return out;
}

/** 单次 publish：返回耗时（ms）；异常时记账并返回 -1。 */
function publishOnce(robot, tsMs) {
  const topic = topicFor(robot.robot_id);
  // mock_fleet.py:157 用 separators=(',',':') 压紧 JSON；这里保持一致，
  // 否则报文大小与真实链路不同，吞吐对照失真。
  const payload = JSON.stringify(robot.buildPayload(tsMs));
  const t0 = Date.now();
  try {
    client.publish(topic, payload, { qos: 0 });
  } catch (err) {
    pubErrors.add(1);
    // 发布异常必须显式 check 失败，而不是静默 continue。
    check(null, {
      'publish 未抛异常': () => false,
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
  // setup 只跑一次，打印本次压测的「合约」——让日志里就有判定依据，
  // 不必事后去猜阈值是从哪来的。
  console.log(
    `[load] broker=${BROKER_URL} robots=${ROBOT_COUNT} hz=${PUBLISH_HZ} ` +
      `vus=${VU_COUNT} duration=${DURATION_S}s ramp=${RAMP_S}s ` +
      `target=${TARGET_MSG_PER_SEC} msg/s expected=${EXPECTED_PUBLISHES} 条`
  );
  console.log(
    `[load] 阈值：连接成功率>=${THRESHOLDS.conn_success_rate} ` +
      `publish p95<${THRESHOLDS.pub_p95_ms}ms p99<${THRESHOLDS.pub_p99_ms}ms ` +
      `吞吐比>=${THRESHOLDS.throughput_ratio}`
  );
  return { startedAt: Date.now() };
}

export default function (data) {
  const vuId = __VU;

  // ① 建连：把「连不上」变成一条可判定的 check，而不是静默跑 0 条。
  if (client === null) {
    robots = myRobotIndices(vuId).map((i) => new Robot(i));
    client = new Client();

    // 事件处理器：error / end 都要留证据（README 的 events 表，
    // 实现见 mqtt/client_on.go:14-20）。只注册一次。
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

    // connect 是同步的（client_connect.go:200-216 token.Wait()），
    // 故这里读 `client.connected` 就是「刚建连完成」的真值。
    const ok = client.connected === true && connectError === null;
    connectOk.add(ok);
    check({ connectError: connectError, connected: client.connected }, {
      'MQTT 连接已建立': (v) => v.connected === true && v.connectError === null,
    });

    if (!ok) {
      console.error(
        `[load] VU ${vuId} 连接失败：${connectError ? connectError.message : 'client.connected=false'}`
      );
      // 连接失败就不发：让 check 的失败率去决定退出码。
      // 不在这里 sleep 重试 —— 重试会把「broker 未就绪」伪装成「慢」。
      sleep(1);
      return;
    }

    vuConnected = true;
    connectedVus.add(1);

    // ② 固定 10Hz 节奏发布。用 setInterval（VU 事件循环，README「Async Programming」），
    //    而不是 `for + sleep` —— sleep 会让 VU 阻塞在 main 函数里，无法响应 end 事件。
    publishTimer = setInterval(() => {
      if (!vuConnected || !client.connected) return;
      const tsMs = Date.now();
      for (const robot of robots) {
        robot.step(PUBLISH_INTERVAL_MS / 1000);
        publishOnce(robot, tsMs);
      }
      tickIndex += 1;
    }, PUBLISH_INTERVAL_MS);

    // ③ 到点收尾：清定时器 → 校验「发布节奏符合预期」→ 主动断开。
    stopTimer = setTimeout(() => {
      if (publishTimer !== null) {
        clearInterval(publishTimer);
        publishTimer = null;
      }

      // 稳态期内本 VU 应发条数：每台机器人 DURATION_S 秒 × PUBLISH_HZ。
      const expectedPerVu = robots.length * PUBLISH_HZ * DURATION_S;
      // 容差 20%：爬坡/收尾各丢几拍属正常，超出就说明定时器被压垮了。
      const low = Math.floor(expectedPerVu * 0.8);
      check({ published: vuPublishes, expected: expectedPerVu, low: low }, {
        'VU 发布条数达到期望的 80%': (v) => v.published >= v.low,
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

  // 主函数空转即可：真正的发布节奏在 setInterval 里。
  // 这里 sleep 的时长决定 VU 在场景结束前是否还在循环 —— 用 1s 足够。
  sleep(1);
}

export function teardown(data) {
  const elapsedS = (Date.now() - data.startedAt) / 1000;
  console.log(`[load] 场景结束，实际用时 ${elapsedS.toFixed(1)}s`);
}

/**
 * handleSummary：把「可判定结果」写进文件，供 CI / 人工核对。
 * 不覆盖 k6 默认的 stdout 摘要（返回对象里的 `stdout`）。
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
    stdout: '\n[load] 判定摘要（checks rate=' + rate.toFixed(4) +
      '，pass=' + pass + ' fail=' + fail + '）\n',
    'tests/load/last-run.json': JSON.stringify(verdict, null, 2) + '\n',
  };
}
