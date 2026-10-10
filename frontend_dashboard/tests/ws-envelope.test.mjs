// 订单 18 T4 验收：用 ajv 校验 ws-envelope.schema.json 能否**抓出故意构造的错误信封**。
// 运行：cd frontend_dashboard && node --test tests/
//
// 这里直接读仓库根的 schema（不经 Vite），确保测的是**契约文件本身**，
// 而不是 wsEnvelope.ts 的加载逻辑。两者读的是同一个文件路径。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import Ajv2020 from 'ajv/dist/2020.js';

const here = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = resolve(here, '../../docs/contracts/ws-envelope.schema.json');
const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf8'));
const ajv = new Ajv2020({ allErrors: true, strict: false });
const validate = ajv.compile(schema);

const joint = { left_shoulder_pitch: 1.0, right_knee_pitch: 2.0 };

/** 一份合法的 telemetry 信封（对照 main.py 的 {"type":"telemetry","data":<state_cache._decode()>}） */
const goodTelemetry = {
  type: 'telemetry',
  data: {
    robot_id: 'humanoid-042',
    status_code: 1,
    error_code: 0,
    battery_pct: 88.1,
    max_joint_temp: 51.2,
    joint_temperatures: joint,
    joint_positions: joint,
    joint_torques: joint,
    ts: 1712000000000,
  },
};

test('契约文件本身存在且是合法 JSON Schema（3 条分支）', () => {
  assert.equal(schema.oneOf.length, 3);
  assert.ok(schema.$defs.AlertEnvelope);
});

test('合法 snapshot / telemetry / alert 三条分支全部通过', () => {
  const snapshot = {
    type: 'snapshot',
    data: { robots: [goodTelemetry.data] },
  };
  const alert = {
    type: 'alert',
    data: { robot_id: 'humanoid-042', rule_id: 'motor_temp', severity: 3, message: '过热', ts: 1712000000001 },
  };
  for (const [name, env] of [['snapshot', snapshot], ['telemetry', goodTelemetry], ['alert', alert]]) {
    assert.equal(validate(env), true, `${name} 应通过，实际错误：${JSON.stringify(validate.errors)}`);
  }
});

test('错误信封 1：telemetry 缺 joint_positions（STATUS.md §六第2条那次真实事故）被拒', () => {
  const bad = structuredClone(goodTelemetry);
  delete bad.data.joint_positions;
  assert.equal(validate(bad), false, '缺 joint_positions 必须被拒（否则又是一条恒空的折线）');
  // ajv 对 `required` 违例的定位放在 params.missingProperty 而不是 instancePath
  // （instancePath 为空串）—— 首次写断言时误以为在 path 里，实测后更正。
  const missing = validate.errors
    .filter((e) => e.keyword === 'required')
    .map((e) => e.params?.missingProperty);
  assert.ok(missing.includes('joint_positions'), `required 错误应点名 joint_positions，实际 ${JSON.stringify(validate.errors)}`);
});

test('错误信封 2：type 不在三分支内被拒', () => {
  const bad = { type: 'unknown_type', data: {} };
  assert.equal(validate(bad), false);
});

test('错误信封 3：status_code 越界（9）被拒', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.status_code = 9;
  assert.equal(validate(bad), false);
});

test('错误信封 4：关节字典的值是字符串（类型漂移）被拒', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.joint_temperatures = { left_shoulder_pitch: '42.1' };
  assert.equal(validate(bad), false);
});

test('错误信封 5：robot_id 不符合 ^humanoid-[0-9]{3}$ 被拒', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.robot_id = 'robot_042';
  assert.equal(validate(bad), false);
});

test('错误信封 6：alert severity 超范围（0）被拒', () => {
  const bad = { type: 'alert', data: { robot_id: 'humanoid-042', rule_id: 'r', severity: 0, message: 'm', ts: 1 } };
  assert.equal(validate(bad), false);
});

test('错误信封 7：信封缺 data 字段被拒', () => {
  assert.equal(validate({ type: 'snapshot' }), false);
});
