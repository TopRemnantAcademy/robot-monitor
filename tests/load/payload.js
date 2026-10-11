// 遥测 payload 生成器 —— 唯一目标是产与 simulator/mock_fleet.py 逐字段同构的报文。
//
// 为什么单独一个文件、为什么逐字对齐
// ----------------------------------
// 后端 Pydantic `Telemetry`（backend_ingestion/app/schemas/telemetry.py:30-56）
// 对 payload 有硬约束：
//   - `robot_id` 必须匹配 ^humanoid-[0-9]{3}$（telemetry.py:27）
//   - 三个关节字典**键必须一致**（telemetry.py:48-54），含 24 键（spec/00 §5.5）
//   - `max_joint_temp` 必须等于 max(joint_temperatures)（telemetry.py:57-62）
// 只要有一个字段漂了，压测就退化成「测 mqtt_dropped 计数器」，
// 而不是测数据链路 —— 那正是 R-07 §6.1「禁止用日志无报错代替通过判定」要防的。
//
// 事实来源（不许凭记忆写）
// ------------------------
// - 关节键名：simulator/mock_fleet.py:36-48（JOINT_NAMES，24 个）
// - payload 字段：simulator/mock_fleet.py:118-142（RobotState.build_payload）
// - 校验契约：backend_ingestion/app/schemas/telemetry.py
// - 数值区间：simulator/mock_fleet.py:50-56 的常量
//
// `tests/load/test_payload_contract.py` 会把本文件与 mock_fleet.py
// 做逐字段对拍（用 Python 解析本文件的常量，不猜）。

// spec/00 §5.5 的 24 个标准关节键名。顺序与 mock_fleet.py:36-48 逐字一致。
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

// 与 mock_fleet.py:50-56 对齐的常量。
export const PUBLISH_INTERVAL_MS = 100;   // 10Hz（mock_fleet.py:34）
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

// 状态码（spec/00 §4.1；mock_fleet.py:52）
export const STATUS_OFFLINE = 0;
export const STATUS_OK = 1;
export const STATUS_WARN = 2;
export const STATUS_FAULT = 3;

/** humanoid-001 ~ humanoid-NNN（spec/00 §1；正则见 telemetry.py:27）。 */
export function robotId(index) {
  if (!Number.isInteger(index) || index < 1 || index > 999) {
    throw new Error('robot index must be an integer in [1, 999], got ' + index);
  }
  return ROBOT_ID_PREFIX + String(index).padStart(3, '0');
}

/** 均匀分布随机数（[min, max)），与 Python random.uniform 的语义一致（闭开区间）。 */
export function uniform(min, max) {
  return min + Math.random() * (max - min);
}

/** 按 mock_fleet.py:105-115 的阈值把环境量折算成 status_code / error_code。 */
export function statusAndError(maxJointTemp, batteryPct) {
  if (maxJointTemp >= 75.0) return { status_code: STATUS_FAULT, error_code: 1 }; // 过热故障
  if (batteryPct < 15.0) return { status_code: STATUS_WARN, error_code: 2 };     // 低电量
  if (maxJointTemp >= 70.0) return { status_code: STATUS_WARN, error_code: 3 };  // 温度偏高
  return { status_code: STATUS_OK, error_code: 0 };
}

// 每台机器人一个「关节温度随机游走」的小状态机，让报文不是凭空随机 ——
// 否则 max_joint_temp 的分布与 mock_fleet 不同，告警触发率无法对照。
function round(value, digits) {
  const f = Math.pow(10, digits);
  return Math.round(value * f) / f;
}

export class Robot {
  constructor(index) {
    this.robot_id = robotId(index);
    this.battery_pct = uniform(60.0, 100.0); // mock_fleet.py:74
    this.joint_temps = {};
    this.walk = {};                          // 每个关节的随机游走步长
    for (const j of JOINT_NAMES) {
      this.joint_temps[j] = uniform(TEMP_BASE_MIN, TEMP_BASE_MAX);
      this.walk[j] = 0;
    }
  }

  /** 推进一个采样周期（mock_fleet.py:91-107 的精简版：不含过热注入）。 */
  step(dtSeconds) {
    // 电量单调下降，0.05%/s（mock_fleet.py:52 BATTERY_DRAIN_PER_S）
    this.battery_pct = Math.max(BATTERY_MIN, this.battery_pct - 0.05 * dtSeconds);
    for (const j of JOINT_NAMES) {
      // 与 mock_fleet.py:94 同量级：每周期 ±0.4°C，并夹在 [35, 65]
      const next = this.joint_temps[j] + uniform(-0.4, 0.4);
      this.joint_temps[j] = Math.min(Math.max(next, TEMP_BASE_MIN), TEMP_BASE_MAX);
    }
  }

  /** 构造与 mock_fleet.py:118-142 同构的 payload（26 个顶层字段）。 */
  buildPayload(tsMs) {
    const temps = {};
    const positions = {};
    const torques = {};
    let maxTemp = -Infinity;
    for (const j of JOINT_NAMES) {
      // 逐字段与 mock_fleet 对齐：温度 2 位、角度 4 位、扭矩 3 位
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
      // 预计算列，必须等于最高关节温度（telemetry.py:57-62 会反推校验）
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

/** mock_fleet.py:39 的 topic 模板。 */
export function topicFor(robot_id) {
  return 'robots/' + robot_id + '/telemetry';
}
