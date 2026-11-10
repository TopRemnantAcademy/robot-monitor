// Order 18 T4 acceptance: use ajv to verify ws-envelope.schema.json can CATCH
// deliberately malformed envelopes.
// Run: cd frontend_dashboard && node --test tests/
//
// Here we read the schema directly from the repo root (not via Vite), to ensure we
// are testing the CONTRACT FILE ITSELF, not the loading logic in wsEnvelope.ts. Both
// read the same file path.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = resolve(here, '../../docs/contracts/ws-envelope.schema.json');
const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf8'));

// [Why not just write `import Ajv2020 from 'ajv/dist/2020.js'`]
// A bare import uses Node's ESM resolution: it walks up from this file's directory
// looking for node_modules, recognizing ONLY the "local node_modules" location.
// Previously the CI frontend deps were GLOBALLY installed (ajv at
// /usr/local/lib/node_modules/ajv, no node_modules in the workspace mount), so the
// ESM resolver never checks the global dir and the real machine errored:
//   Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'ajv'
//   imported from /workspace/frontend_dashboard/tests/ws-envelope.test.mjs
//   (build cnb-1tc-1k4k4ng86-002).
// Tried NODE_PATH: INEFFECTIVE — it only affects CJS require; the ESM resolver
// ignores it entirely (verified). CI now does a runtime `npm ci`
// (ci/contract-gate.sh stage 2) so local node_modules is present and a bare import
// would work; we still resolve explicitly to be INDEPENDENT of the install method:
// use createRequire (CJS resolver, honors the global dir) to locate the entry in
// "local -> global" order, then pathToFileURL to turn it into an ESM-importable
// file:// URL — same approach as gen-types.mjs, so both sides (local / CI, local
// install / global install) behave identically.
const require_ = createRequire(import.meta.url);
const AJV_CANDIDATES = [
  join(here, '..', 'node_modules', 'ajv', 'dist', '2020.js'),
  join('/usr/local/lib/node_modules', 'ajv', 'dist', '2020.js'),
];
let ajvEntry = null;
for (const candidate of AJV_CANDIDATES) {
  try {
    ajvEntry = require_.resolve(candidate);
    break;
  } catch {
    // try the next candidate location
  }
}
if (!ajvEntry) {
  console.error('[ws-envelope] ajv/dist/2020.js not found; tried:');
  for (const c of AJV_CANDIDATES) console.error('  - ' + c);
  console.error('[ws-envelope] local: cd frontend_dashboard && npm ci');
  process.exit(1);
}
// Dynamic import to get the default export (ajv 8.x is CJS; ESM side default is the
// Ajv constructor).
const { default: Ajv2020 } = await import(pathToFileURL(ajvEntry).href);
const ajv = new Ajv2020({ allErrors: true, strict: false });
const validate = ajv.compile(schema);

const joint = { left_shoulder_pitch: 1.0, right_knee_pitch: 2.0 };

/** A valid telemetry envelope (matches main.py's {"type":"telemetry","data":<state_cache._decode()>}) */
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
    // ISSUE #77: is_online is now required — the frontend only uses it to judge
    // online status; without it the entire telemetry envelope is rejected by ajv
    // (which is what we want: better to error at dev time than let "online state"
    // silently fall back to guessing from status_code).
    is_online: true,
    last_seen: 1712000000,
  },
};

test('contract file exists and is a valid JSON Schema (3 branches)', () => {
  assert.equal(schema.oneOf.length, 3);
  assert.ok(schema.$defs.AlertEnvelope);
});

test('valid snapshot / telemetry / alert branches all pass', () => {
  const snapshot = {
    type: 'snapshot',
    data: { robots: [goodTelemetry.data] },
  };
  const alert = {
    type: 'alert',
    data: { robot_id: 'humanoid-042', rule_id: 'motor_temp', severity: 3, message: 'overheat', ts: 1712000000001 },
  };
  for (const [name, env] of [['snapshot', snapshot], ['telemetry', goodTelemetry], ['alert', alert]]) {
    assert.equal(validate(env), true, `${name} should pass; actual errors: ${JSON.stringify(validate.errors)}`);
  }
});

test('bad envelope 1: telemetry missing joint_positions (the real STATUS.md §6.2 incident) is rejected', () => {
  const bad = structuredClone(goodTelemetry);
  delete bad.data.joint_positions;
  assert.equal(validate(bad), false, 'missing joint_positions must be rejected (otherwise another permanently-empty line)');
  // ajv reports `required` violations via params.missingProperty, not instancePath
  // (instancePath is an empty string) — the first assertion write mistakenly looked in
  // the path; corrected after verification.
  const missing = validate.errors
    .filter((e) => e.keyword === 'required')
    .map((e) => e.params?.missingProperty);
  assert.ok(missing.includes('joint_positions'), `required error should name joint_positions; actual ${JSON.stringify(validate.errors)}`);
});

test('bad envelope 2: type outside the three branches is rejected', () => {
  const bad = { type: 'unknown_type', data: {} };
  assert.equal(validate(bad), false);
});

test('bad envelope 3: status_code out of range (9) is rejected', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.status_code = 9;
  assert.equal(validate(bad), false);
});

test('bad envelope 4: joint dict value is a string (type drift) is rejected', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.joint_temperatures = { left_shoulder_pitch: '42.1' };
  assert.equal(validate(bad), false);
});

test('bad envelope 5: robot_id not matching ^humanoid-[0-9]{3}$ is rejected', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.robot_id = 'robot_042';
  assert.equal(validate(bad), false);
});

test('bad envelope 6: alert severity out of range (0) is rejected', () => {
  const bad = { type: 'alert', data: { robot_id: 'humanoid-042', rule_id: 'r', severity: 0, message: 'm', ts: 1 } };
  assert.equal(validate(bad), false);
});

test('bad envelope 7: envelope missing data field is rejected', () => {
  assert.equal(validate({ type: 'snapshot' }), false);
});

test('bad envelope 8 (ISSUE #77): telemetry missing is_online is rejected', () => {
  const bad = structuredClone(goodTelemetry);
  delete bad.data.is_online;
  assert.equal(validate(bad), false, 'missing is_online must be rejected — otherwise online judgment silently falls back to status_code guessing');
  const missing = validate.errors
    .filter((e) => e.keyword === 'required')
    .map((e) => e.params?.missingProperty);
  assert.ok(missing.includes('is_online'), `required error should name is_online; actual ${JSON.stringify(validate.errors)}`);
});

test('bad envelope 9 (ISSUE #77): is_online as string "false" is rejected (type drift would make Boolean() always true)', () => {
  const bad = structuredClone(goodTelemetry);
  bad.data.is_online = 'false';
  assert.equal(validate(bad), false);
});

test('valid variant (ISSUE #77): last_seen allowed to be null (never reported)', () => {
  const env = structuredClone(goodTelemetry);
  env.data.is_online = false;
  env.data.last_seen = null;
  assert.equal(validate(env), true, `last_seen=null should pass; actual: ${JSON.stringify(validate.errors)}`);
});
