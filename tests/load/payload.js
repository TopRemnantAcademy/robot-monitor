// Telemetry payload generator —— sole goal is to produce messages structurally identical
// field-by-field to simulator/mock_fleet.py.
//
// Why a separate file, and why byte-align
// ---------------------------------------
// The backend Pydantic `Telemetry` (backend_ingestion/app/schemas/telemetry.py:30-56) has hard
// constraints on the payload:
//   - `robot_id` must match ^humanoid-[0-9]{3}$ (telemetry.py:27)
//   - the three joint dicts must have **consistent keys** (telemetry.py:48-54), 24 keys (spec/00 §5.5)
//   - `max_joint_temp` must equal max(joint_temperatures) (telemetry.py:57-62)
// If a single field drifts, the load test degenerates into "testing the mqtt_dropped counter"
// instead of the data link —— exactly what R-07 §6.1 "forbid using no-errors-in-logs as a pass
// judgment" guards against.
//
// Sources of truth (don't write from memory)
// ------------------------------------------
// - joint key names: simulator/mock_fleet.py:36-48 (JOINT_NAMES, 24)
// - payload fields: simulator/mock_fleet.py:118-142 (RobotState.build_payload)
// - validation contract: backend_ingestion/app/schemas/telemetry.py
// - value ranges: simulator/mock_fleet.py:50-56 constants
//
// `tests/load/test_payload_contract.py` does a field-by-field diff of this file against
// mock_fleet.py (parses this file's constants with Python, doesn't guess).

// The 24 standard joint key names from spec/00 §5.5. Order matches mock_fleet.py:36-48 verbatim.
export const JOINT_NAMES = [
  'left_shoulder_pitch', 'left_shoulder_roll', 'left_shoulder_yaw', 'left_elbow_pitch',
  'left_wrist_pitch', 'left_wrist_roll',
  'right_shoulder_pitch', 'right_shoulder_roll', 'right_shoulder_yaw', 'right_elbow_pitch',
  'right_wrist_pitch', 'right_wrist_roll',
  'left_hip_yaw', 'left_hip_roll', 'left_hip_pitch',
  'left_knee_pitch', 'left_ankle_pitch', 'left_ankle_roll',
  'right_hip_yaw', 'right_hip_roll', 'right_hip_pitch',
  'right_knee_pitch', 'right_ankle_pitch', 'right_ankle_roll',
];

// Constants aligned with mock_fleet.py:50-56.
export const PUBLISH_INTERVAL_MS = 100;   // 10Hz (mock_fleet.py:34)
export const ROBOT_ID_PREFIX = 'humanoid-';
export const TEMP_BASE_MIN = 35.0;        // mock_fleet.py:55 TEMP_BASE_RANGE[0]
export const TEMP_BASE_MAX = 65.0;        // mock_fleet.py:55 TEMP_BASE_RANGE[1]
export const BATTERY_MIN = 0.0;
export const BATTERY_MAX = 100.0;
export const BATTERY_TEMP_MIN = 28.0;     // mock_fleet.py:132
export const BATTERY_TEMP_MAX = 45.0;     // mock_fleet.py:132
export const POSITION_RANGE = 1.5;        // mock_fleet.py:130 joint_positions ∈ [-1.5, 1.5]
export const TORQUE_MIN = 0.0;            // mock_fleet.py:131
export const TORQUE_MAX = 12.0;           // mock_fleet.py:131

// Status codes (spec/00 §4.1; mock_fleet.py:52)
export const STATUS_OFFLINE = 0;
export const STATUS_OK = 1;
export const STATUS_WARN = 2;
export const STATUS_FAULT = 3;

/** humanoid-001 ~ humanoid-NNN (spec/00 §1; regex see telemetry.py:27). */
export function robotId(index) {
  if (!Number.isInteger(index) || index < 1 || index > 999) {
    throw new Error('robot index must be an integer in [1, 999], got ' + index);
  }
  return ROBOT_ID_PREFIX + String(index).padStart(3, '0');
}

/** Uniform random number ([min, max)), consistent with Python random.uniform semantics (half-open). */
export function uniform(min, max) {
  return min + Math.random() * (max - min);
}

/** Map environment quantities to status_code / error_code per mock_fleet.py:105-115 thresholds. */
export function statusAndError(maxJointTemp, batteryPct) {
  if (maxJointTemp >= 75.0) return { status_code: STATUS_FAULT, error_code: 1 }; // overheat fault
  if (batteryPct < 15.0) return { status_code: STATUS_WARN, error_code: 2 };     // low battery
  if (maxJointTemp >= 70.0) return { status_code: STATUS_WARN, error_code: 3 };  // high temperature
  return { status_code: STATUS_OK, error_code: 0 };
}

// A small state machine of "joint-temperature random walk" per robot, so the messages aren't
// random from nothing —— otherwise max_joint_temp's distribution differs from mock_fleet, and alert
// trigger rates can't be compared.
function round(value, digits) {
  const f = Math.pow(10, digits);
  return Math.round(value * f) / f;
}

export class Robot {
  constructor(index) {
    this.robot_id = robotId(index);
    this.battery_pct = uniform(60.0, 100.0); // mock_fleet.py:74
    this.joint_temps = {};
    this.walk = {};                          // random-walk step per joint
    for (const j of JOINT_NAMES) {
      this.joint_temps[j] = uniform(TEMP_BASE_MIN, TEMP_BASE_MAX);
      this.walk[j] = 0;
    }
  }

  /** Advance one sampling period (trimmed version of mock_fleet.py:91-107: no overheat injection). */
  step(dtSeconds) {
    // Battery monotonically decreases, 0.05%/s (mock_fleet.py:52 BATTERY_DRAIN_PER_S)
    this.battery_pct = Math.max(BATTERY_MIN, this.battery_pct - 0.05 * dtSeconds);
    for (const j of JOINT_NAMES) {
      // Same order of magnitude as mock_fleet.py:94: ±0.4°C per period, clamped to [35, 65]
      const next = this.joint_temps[j] + uniform(-0.4, 0.4);
      this.joint_temps[j] = Math.min(Math.max(next, TEMP_BASE_MIN), TEMP_BASE_MAX);
    }
  }

  /** Build the payload structurally identical to mock_fleet.py:118-142 (26 top-level fields). */
  buildPayload(tsMs) {
    const temps = {};
    const positions = {};
    const torques = {};
    let maxTemp = -Infinity;
    for (const j of JOINT_NAMES) {
      // Field-by-field aligned with mock_fleet: temperature 2 dp, angle 4 dp, torque 3 dp
      const t = round(this.joint_temps[j], 2);
      temps[j] = t;
      if (t > maxTemp) maxTemp = t;
      positions[j] = round(uniform(-POSITION_RANGE, POSITION_RANGE), 4);
      torques[j] = round(uniform(TORQUE_MIN, TORQUE_MAX), 3);
    }
    const { status_code, error_code } = statusAndError(maxTemp, this.battery_pct);
    return {
      robot_id: this.robot_id,
      status_code: status_code,
      error_code: error_code,
      battery_pct: round(this.battery_pct, 2),
      battery_temp: round(uniform(BATTERY_TEMP_MIN, BATTERY_TEMP_MAX), 2),
      // Precomputed column, must equal the highest joint temperature (telemetry.py:57-62 re-derives).
      max_joint_temp: round(maxTemp, 2),
      joint_temperatures: temps,
      joint_positions: positions,
      joint_torques: torques,
      pos_x: round(uniform(0.0, 50.0), 3),
      pos_y: round(uniform(0.0, 30.0), 3),
      pos_z: round(uniform(0.0, 3.0), 3),
      orient_w: round(uniform(-1.0, 1.0), 4),
      orient_x: round(uniform(-1.0, 1.0), 4),
      orient_y: round(uniform(-1.0, 1.0), 4),
      orient_z: round(uniform(-1.0, 1.0), 4),
      ts: tsMs,
    };
  }
}

/** mock_fleet.py:39 topic template. */
export function topicFor(robot_id) {
  return 'robots/' + robot_id + '/telemetry';
}
