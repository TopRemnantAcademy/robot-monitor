//! Phase 0 + 1 Proof-of-Concept: ISO 21423:2026 envelope mapper in Rust.
//!
//! Phase 0 (envelope mapper): a faithful port of `bridge/_iso21423_py.py`.
//! Exposes `OperatingMode`, `BatteryState`, `Pose`, `Velocity`,
//! `ISO21423RobotState`, `yaw_to_quat`, `telemetry_to_iso21423`,
//! `iso21423_to_telemetry` so `bridge/iso21423.py` can re-export it and the
//! existing `bridge/tests/` suite keeps passing unchanged.
//!
//! Phase 1 (ROS 2 hot path): `Ros2StateAccumulator` ports the per-message ROS 2
//! JSON -> ISO 21423 envelope accumulator (the CPU hot path inside
//! `RosbridgeClient._handle`). WebSocket I/O stays in Python; only the parsing
//! is moved to Rust. `bridge/ros2_bridge.py` shims it so
//! `bridge/tests/test_ros2_bridge.py` keeps passing unchanged.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyType};
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

// --------------------------------------------------------------------------
// Rounding that mirrors CPython's `float.__round__` (round-half-to-even via the
// pow(10, n) scaling CPython itself uses), so rounded floats bit-match Python.
// --------------------------------------------------------------------------
fn py_round(x: f64, ndigits: i32) -> f64 {
    if ndigits >= 0 {
        let p = 10f64.powi(ndigits);
        let y = x * p;
        let mut z = y.round();
        if (y - z).abs() == 0.5 {
            z = 2.0 * (y / 2.0).round();
        }
        z / p
    } else {
        let p = 10f64.powi(-ndigits);
        let y = x / p;
        let mut z = y.round();
        if (y - z).abs() == 0.5 {
            z = 2.0 * (y / 2.0).round();
        }
        z * p
    }
}

// --------------------------------------------------------------------------
// Field extraction helpers (mirror `t.get(key) or default` semantics).
// --------------------------------------------------------------------------
fn get_f64(t: &Bound<PyDict>, key: &str, default: f64) -> f64 {
    match t.get_item(key) {
        Ok(Some(v)) => {
            if v.is_none() {
                default
            } else {
                v.extract::<f64>().unwrap_or(default)
            }
        }
        _ => default,
    }
}

fn get_i64(t: &Bound<PyDict>, key: &str, default: i64) -> i64 {
    match t.get_item(key) {
        Ok(Some(v)) => {
            if v.is_none() {
                default
            } else if let Ok(i) = v.extract::<i64>() {
                i
            } else if let Ok(f) = v.extract::<f64>() {
                f as i64
            } else {
                default
            }
        }
        _ => default,
    }
}

fn get_str(t: &Bound<PyDict>, key: &str, default: &str) -> String {
    match t.get_item(key) {
        Ok(Some(v)) => {
            if v.is_none() {
                default.to_string()
            } else {
                v.extract::<String>().unwrap_or_else(|_| default.to_string())
            }
        }
        _ => default.to_string(),
    }
}

fn get_f64_map(t: &Bound<PyDict>, key: &str) -> HashMap<String, f64> {
    match t.get_item(key) {
        Ok(Some(v)) => {
            if v.is_none() {
                HashMap::new()
            } else if let Ok(d) = v.downcast::<PyDict>() {
                let mut m = HashMap::new();
                for (k, val) in d.iter() {
                    let kk = k.extract::<String>().unwrap_or_default();
                    let vv = if val.is_none() {
                        0.0
                    } else {
                        val.extract::<f64>().unwrap_or(0.0)
                    };
                    m.insert(kk, vv);
                }
                m
            } else {
                HashMap::new()
            }
        }
        _ => HashMap::new(),
    }
}

// --------------------------------------------------------------------------
// Shared normalize (single source of truth for OperatingMode.normalize and the
// ROS 2 accumulator's mode ingestion). Mirrors the Python reference exactly:
// upper-case the (possibly None) value; if it is a known member return it,
// otherwise fall back to AUTOMATIC.
// --------------------------------------------------------------------------
fn normalize_mode(value: Option<String>) -> String {
    match value {
        Some(v) => {
            let up = v.to_uppercase();
            if matches!(
                up.as_str(),
                "IDLE" | "AUTOMATIC" | "MANUAL" | "SERVICE" | "ERROR"
            ) {
                up
            } else {
                "AUTOMATIC".to_string()
            }
        }
        None => "AUTOMATIC".to_string(),
    }
}

// Walk into a nested dict by key, returning an empty dict when absent/non-dict
// (mirrors `(msg.get(k) or {}).get(...) or {}` chains in the Python original).
fn subdict<'py>(py: Python<'py>, d: &Bound<'py, PyDict>, key: &str) -> Bound<'py, PyDict> {
    match d.get_item(key) {
        Ok(Some(v)) => v
            .downcast::<PyDict>()
            .map(|dd| dd.clone())
            .unwrap_or_else(|_| PyDict::new(py)),
        _ => PyDict::new(py),
    }
}

// Extract a JSON array (list of Any) for a key, or an empty list when absent.
fn get_list<'py>(d: &Bound<'py, PyDict>, key: &str) -> Vec<Bound<'py, PyAny>> {
    match d.get_item(key) {
        Ok(Some(v)) => {
            if v.is_none() {
                Vec::new()
            } else if let Ok(list) = v.downcast::<PyList>() {
                list.iter().collect()
            } else {
                Vec::new()
            }
        }
        _ => Vec::new(),
    }
}

// --------------------------------------------------------------------------
// Mapper dataclasses (kept as real pyclasses so tests can construct + read them
// directly). `ISO21423RobotState` stores `battery`/`pose`/`velocity` as generic
// Python objects (duck-typed) to stay faithful to the Python original, where
// `ros2_bridge` feeds it a `BatteryLike` rather than a `BatteryState`.
// --------------------------------------------------------------------------
#[pyclass]
#[derive(Clone, Debug, Default)]
struct BatteryState {
    #[pyo3(get, set)]
    charge_pct: f64,
    #[pyo3(get, set)]
    voltage_v: f64,
    #[pyo3(get, set)]
    current_a: f64,
    #[pyo3(get, set)]
    temperature_c: f64,
    #[pyo3(get, set)]
    power_supply_status: i64,
}

#[pymethods]
impl BatteryState {
    #[new]
    #[pyo3(signature = (charge_pct=0.0, voltage_v=0.0, current_a=0.0, temperature_c=0.0, power_supply_status=0))]
    fn new(
        charge_pct: f64,
        voltage_v: f64,
        current_a: f64,
        temperature_c: f64,
        power_supply_status: i64,
    ) -> Self {
        BatteryState {
            charge_pct,
            voltage_v,
            current_a,
            temperature_c,
            power_supply_status,
        }
    }
}

#[pyclass]
#[derive(Clone, Debug, Default)]
struct Pose {
    #[pyo3(get, set)]
    x: f64,
    #[pyo3(get, set)]
    y: f64,
    #[pyo3(get, set)]
    z: f64,
    #[pyo3(get, set)]
    qw: f64,
    #[pyo3(get, set)]
    qx: f64,
    #[pyo3(get, set)]
    qy: f64,
    #[pyo3(get, set)]
    qz: f64,
    #[pyo3(get, set)]
    map_id: String,
}

#[pymethods]
impl Pose {
    #[new]
    #[pyo3(signature = (x=0.0, y=0.0, z=0.0, qw=1.0, qx=0.0, qy=0.0, qz=0.0, map_id=String::new()))]
    fn new(
        x: f64,
        y: f64,
        z: f64,
        qw: f64,
        qx: f64,
        qy: f64,
        qz: f64,
        map_id: String,
    ) -> Self {
        Pose {
            x,
            y,
            z,
            qw,
            qx,
            qy,
            qz,
            map_id,
        }
    }
}

#[pyclass]
#[derive(Clone, Debug, Default)]
struct Velocity {
    #[pyo3(get, set)]
    vx: f64,
    #[pyo3(get, set)]
    vy: f64,
    #[pyo3(get, set)]
    omega: f64,
}

#[pymethods]
impl Velocity {
    #[new]
    #[pyo3(signature = (vx=0.0, vy=0.0, omega=0.0))]
    fn new(vx: f64, vy: f64, omega: f64) -> Self {
        Velocity { vx, vy, omega }
    }
}

#[pyclass(unsendable)]
#[derive(Debug)]
struct ISO21423RobotState {
    #[pyo3(get, set)]
    version: String,
    #[pyo3(get, set)]
    manufacturer: String,
    #[pyo3(get, set)]
    serial_number: String,
    #[pyo3(get, set)]
    timestamp_ms: i64,
    #[pyo3(get, set)]
    operating_mode: String,
    #[pyo3(get, set)]
    battery: Py<PyAny>,
    #[pyo3(get, set)]
    pose: Py<PyAny>,
    #[pyo3(get, set)]
    velocity: Py<PyAny>,
    #[pyo3(get, set)]
    joint_positions: HashMap<String, f64>,
    #[pyo3(get, set)]
    joint_torques: HashMap<String, f64>,
    #[pyo3(get, set)]
    condition: HashMap<String, f64>,
    #[pyo3(get, set)]
    errors: Vec<String>,
}

#[pymethods]
impl ISO21423RobotState {
    #[new]
    #[pyo3(signature = (
        battery,
        pose,
        velocity,
        version=String::new(),
        manufacturer=String::new(),
        serial_number=String::new(),
        timestamp_ms=0,
        operating_mode=String::new(),
        joint_positions=HashMap::new(),
        joint_torques=HashMap::new(),
        condition=HashMap::new(),
        errors=Vec::new(),
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        battery: Py<PyAny>,
        pose: Py<PyAny>,
        velocity: Py<PyAny>,
        version: String,
        manufacturer: String,
        serial_number: String,
        timestamp_ms: i64,
        operating_mode: String,
        joint_positions: HashMap<String, f64>,
        joint_torques: HashMap<String, f64>,
        condition: HashMap<String, f64>,
        errors: Vec<String>,
    ) -> Self {
        ISO21423RobotState {
            version,
            manufacturer,
            serial_number,
            timestamp_ms,
            operating_mode,
            battery,
            pose,
            velocity,
            joint_positions,
            joint_torques,
            condition,
            errors,
        }
    }
}

#[pyclass]
struct OperatingMode;

#[pymethods]
#[allow(non_snake_case)]
impl OperatingMode {
    #[classattr]
    fn IDLE() -> &'static str {
        "IDLE"
    }
    #[classattr]
    fn AUTOMATIC() -> &'static str {
        "AUTOMATIC"
    }
    #[classattr]
    fn MANUAL() -> &'static str {
        "MANUAL"
    }
    #[classattr]
    fn SERVICE() -> &'static str {
        "SERVICE"
    }
    #[classattr]
    fn ERROR() -> &'static str {
        "ERROR"
    }

    #[classmethod]
    #[pyo3(signature = (value=None))]
    fn normalize(_cls: &Bound<'_, PyType>, value: Option<String>) -> String {
        normalize_mode(value)
    }

    #[classmethod]
    #[pyo3(signature = (mode))]
    fn severity(_cls: &Bound<'_, PyType>, mode: String) -> i64 {
        match mode.as_str() {
            "IDLE" | "AUTOMATIC" | "MANUAL" => 1,
            "SERVICE" => 2,
            "ERROR" => 3,
            _ => 1,
        }
    }
}

#[pyfunction]
fn yaw_to_quat(theta: f64) -> (f64, f64, f64, f64) {
    let half = theta / 2.0;
    (
        py_round(half.cos(), 6),
        0.0,
        0.0,
        py_round(half.sin(), 6),
    )
}

#[pyfunction]
#[pyo3(signature = (t, manufacturer = "robotmonitor-sim".to_string(), version = "1.0".to_string()))]
fn telemetry_to_iso21423(
    py: Python<'_>,
    t: &Bound<PyDict>,
    manufacturer: String,
    version: String,
) -> PyResult<Py<ISO21423RobotState>> {
    let sc = get_i64(t, "status_code", 1);
    let ec = get_i64(t, "error_code", 0);
    let operating_mode = if sc == 3 || ec != 0 {
        "ERROR".to_string()
    } else if sc == 2 {
        "SERVICE".to_string()
    } else {
        "AUTOMATIC".to_string()
    };

    let battery = Bound::new(
        py,
        BatteryState {
            charge_pct: get_f64(t, "battery_pct", 0.0),
            voltage_v: 0.0,
            current_a: 0.0,
            temperature_c: get_f64(t, "battery_temp", 0.0),
            power_supply_status: 0,
        },
    )?
    .into_any()
    .unbind();

    let pose = Bound::new(
        py,
        Pose {
            x: get_f64(t, "pos_x", 0.0),
            y: get_f64(t, "pos_y", 0.0),
            z: get_f64(t, "pos_z", 0.0),
            qw: get_f64(t, "orient_w", 1.0),
            qx: get_f64(t, "orient_x", 0.0),
            qy: get_f64(t, "orient_y", 0.0),
            qz: get_f64(t, "orient_z", 0.0),
            map_id: String::new(),
        },
    )?
    .into_any()
    .unbind();

    let velocity = Bound::new(py, Velocity::default())?.into_any().unbind();

    Ok(Bound::new(
        py,
        ISO21423RobotState {
            version,
        manufacturer,
        serial_number: get_str(t, "robot_id", ""),
        timestamp_ms: get_i64(t, "ts", 0),
        operating_mode,
        battery,
        pose,
        velocity,
        joint_positions: get_f64_map(t, "joint_positions"),
        joint_torques: get_f64_map(t, "joint_torques"),
        condition: get_f64_map(t, "joint_temperatures"),
        errors: if ec == 0 {
            Vec::new()
        } else {
            vec![format!("error_code={}", ec)]
        },
    })?
    .unbind())
}

fn attr_f64(py: Python<'_>, obj: &Py<PyAny>, name: &str) -> PyResult<f64> {
    obj.bind(py).getattr(name)?.extract::<f64>()
}

#[pyfunction]
fn iso21423_to_telemetry(py: Python<'_>, state: Bound<ISO21423RobotState>) -> Py<PyDict> {
    let s = state.borrow();
    let mode = &s.operating_mode;
    let (status_code, error_code) = if mode == "ERROR" || !s.errors.is_empty() {
        (3, if s.errors.is_empty() { 0 } else { 1 })
    } else if mode == "SERVICE" {
        (2, 0)
    } else {
        (1, 0)
    };

    let d = PyDict::new(py);
    d.set_item("robot_id", &s.serial_number).unwrap();
    d.set_item("status_code", status_code).unwrap();
    d.set_item("error_code", error_code).unwrap();

    let charge_pct = attr_f64(py, &s.battery, "charge_pct").unwrap_or(0.0);
    d.set_item("battery_pct", py_round(charge_pct, 2)).unwrap();

    let temp_c = attr_f64(py, &s.battery, "temperature_c").unwrap_or(0.0);
    if temp_c != 0.0 {
        d.set_item("battery_temp", py_round(temp_c, 2)).unwrap();
    } else {
        d.set_item("battery_temp", py.None()).unwrap();
    }

    let temps = &s.condition;
    let max_temp = if temps.is_empty() {
        None
    } else {
        Some(temps.values().cloned().fold(f64::NEG_INFINITY, f64::max))
    };
    match max_temp {
        Some(mt) => {
            d.set_item("max_joint_temp", py_round(mt, 2)).unwrap();
        }
        None => {
            d.set_item("max_joint_temp", py.None()).unwrap();
        }
    }

    let jt: HashMap<String, f64> = temps
        .iter()
        .map(|(k, v)| (k.clone(), py_round(*v, 2)))
        .collect();
    d.set_item("joint_temperatures", jt).unwrap();

    let jp: HashMap<String, f64> = s
        .joint_positions
        .iter()
        .map(|(k, v)| (k.clone(), py_round(*v, 4)))
        .collect();
    d.set_item("joint_positions", jp).unwrap();

    let jq: HashMap<String, f64> = s
        .joint_torques
        .iter()
        .map(|(k, v)| (k.clone(), py_round(*v, 3)))
        .collect();
    d.set_item("joint_torques", jq).unwrap();

    let p = s.pose.bind(py);
    let px = p.getattr("x").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);
    let py_y = p.getattr("y").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);
    let pz = p.getattr("z").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);
    let qw = p.getattr("qw").and_then(|v| v.extract::<f64>()).unwrap_or(1.0);
    let qx = p.getattr("qx").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);
    let qy = p.getattr("qy").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);
    let qz = p.getattr("qz").and_then(|v| v.extract::<f64>()).unwrap_or(0.0);

    d.set_item("pos_x", py_round(px, 3)).unwrap();
    d.set_item("pos_y", py_round(py_y, 3)).unwrap();
    d.set_item("pos_z", py_round(pz, 3)).unwrap();
    d.set_item("orient_w", py_round(qw, 4)).unwrap();
    d.set_item("orient_x", py_round(qx, 4)).unwrap();
    d.set_item("orient_y", py_round(qy, 4)).unwrap();
    d.set_item("orient_z", py_round(qz, 4)).unwrap();

    d.set_item("ts", s.timestamp_ms).unwrap();

    d.unbind()
}

// --------------------------------------------------------------------------
// Phase 1: ROS 2 message accumulator (the hot path inside RosbridgeClient).
//
// Pure (no I/O): accumulates ROS 2 messages (plain JSON dicts) for one robot
// into an `ISO21423RobotState` envelope. This is a faithful port of
// `bridge/ros2_bridge.py`'s `ROS2StateAccumulator` + the per-message-type
// handlers. Keeping the field extraction in Rust moves the per-frame CPU work
// out of Python; the WebSocket transport stays in Python.
// --------------------------------------------------------------------------
#[pyclass]
#[derive(Debug, Default)]
struct Ros2StateAccumulator {
    battery_charge_pct: f64,
    battery_voltage_v: f64,
    battery_current_a: f64,
    battery_temperature_c: f64,
    battery_power_supply_status: i64,
    // x, y, z, qw, qx, qy, qz, map_id
    pose: Option<(f64, f64, f64, f64, f64, f64, f64, String)>,
    // vx, vy, omega
    velocity: Option<(f64, f64, f64)>,
    joint_positions: HashMap<String, f64>,
    joint_torques: HashMap<String, f64>,
    condition: HashMap<String, f64>,
    mode: String,
    errors: Vec<String>,
}

impl Ros2StateAccumulator {
    fn apply_pose(&mut self, pos: &Bound<PyDict>, ori: &Bound<PyDict>) {
        self.pose = Some((
            get_f64(pos, "x", 0.0),
            get_f64(pos, "y", 0.0),
            get_f64(pos, "z", 0.0),
            get_f64(ori, "w", 1.0),
            get_f64(ori, "x", 0.0),
            get_f64(ori, "y", 0.0),
            get_f64(ori, "z", 0.0),
            String::new(),
        ));
    }

    fn h_battery(&mut self, msg: &Bound<PyDict>) -> PyResult<()> {
        let pct = match msg.get_item("percentage")? {
            Some(v) if !v.is_none() => {
                let f = v.extract::<f64>().unwrap_or(0.0);
                if f <= 1.0 {
                    f * 100.0
                } else {
                    f
                }
            }
            _ => 0.0,
        };
        self.battery_charge_pct = pct;
        self.battery_voltage_v = get_f64(msg, "voltage", 0.0);
        self.battery_current_a = get_f64(msg, "current", 0.0);
        self.battery_temperature_c = get_f64(msg, "temperature", 0.0);
        self.battery_power_supply_status = get_i64(msg, "power_supply_status", 0);
        Ok(())
    }

    fn h_joint_state(&mut self, msg: &Bound<PyDict>) -> PyResult<()> {
        let names = get_list(msg, "name");
        let positions = get_list(msg, "position");
        let efforts = get_list(msg, "effort");
        for (i, name_val) in names.iter().enumerate() {
            let name = name_val.extract::<String>().unwrap_or_default();
            if i < positions.len() {
                let pv = &positions[i];
                let f = if pv.is_none() {
                    0.0
                } else {
                    pv.extract::<f64>().unwrap_or(0.0)
                };
                self.joint_positions.insert(name.clone(), f);
            }
            if i < efforts.len() {
                let ev = &efforts[i];
                let f = if ev.is_none() {
                    0.0
                } else {
                    ev.extract::<f64>().unwrap_or(0.0)
                };
                self.joint_torques.insert(name.clone(), f);
            }
        }
        Ok(())
    }

    fn h_odometry(&mut self, py: Python<'_>, msg: &Bound<PyDict>) -> PyResult<()> {
        let pose1 = subdict(py, &subdict(py, msg, "pose"), "pose");
        let pos = subdict(py, &pose1, "position");
        let ori = subdict(py, &pose1, "orientation");
        self.apply_pose(&pos, &ori);
        let twist = subdict(py, &subdict(py, msg, "twist"), "twist");
        let lin = subdict(py, &twist, "linear");
        let ang = subdict(py, &twist, "angular");
        self.velocity = Some((
            get_f64(&lin, "x", 0.0),
            get_f64(&lin, "y", 0.0),
            get_f64(&ang, "z", 0.0),
        ));
        Ok(())
    }

    fn h_pose_stamped(&mut self, py: Python<'_>, msg: &Bound<PyDict>) -> PyResult<()> {
        let pose0 = subdict(py, msg, "pose");
        let pos = subdict(py, &pose0, "position");
        let ori = subdict(py, &pose0, "orientation");
        self.apply_pose(&pos, &ori);
        Ok(())
    }

    fn h_string_mode(&mut self, msg: &Bound<PyDict>) -> PyResult<()> {
        let data = get_str(msg, "data", "");
        if !data.is_empty() {
            self.mode = normalize_mode(Some(data));
        }
        Ok(())
    }
}

#[pymethods]
impl Ros2StateAccumulator {
    #[new]
    fn new() -> Self {
        Ros2StateAccumulator {
            mode: "AUTOMATIC".to_string(),
            ..Default::default()
        }
    }

    /// Dispatch a single ROS 2 message (delivered as a plain JSON dict by
    /// rosbridge) to the right per-type handler. Unknown types are ignored.
    fn ingest(
        &mut self,
        py: Python<'_>,
        msg_type: String,
        msg: &Bound<PyDict>,
    ) -> PyResult<()> {
        match msg_type.as_str() {
            "sensor_msgs/msg/BatteryState" => self.h_battery(msg),
            "sensor_msgs/msg/JointState" => self.h_joint_state(msg),
            "nav_msgs/msg/Odometry" => self.h_odometry(py, msg),
            "geometry_msgs/msg/PoseStamped" | "geometry_msgs/msg/PoseWithCovarianceStamped" => {
                self.h_pose_stamped(py, msg)
            }
            "std_msgs/msg/String" => self.h_string_mode(msg),
            _ => Ok(()),
        }
    }

    #[pyo3(signature = (serial_number, manufacturer, version="1.0".to_string(), timestamp_ms=None))]
    fn build(
        &self,
        py: Python<'_>,
        serial_number: String,
        manufacturer: String,
        version: String,
        timestamp_ms: Option<i64>,
    ) -> PyResult<Py<ISO21423RobotState>> {
        let battery = Bound::new(
            py,
            BatteryState {
                charge_pct: self.battery_charge_pct,
                voltage_v: self.battery_voltage_v,
                current_a: self.battery_current_a,
                temperature_c: self.battery_temperature_c,
                power_supply_status: self.battery_power_supply_status,
            },
        )?
        .into_any()
        .unbind();

        let (px, py_, pz, pqw, pqx, pqy, pqz, pmap) = self
            .pose
            .clone()
            .unwrap_or((0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, String::new()));
        let pose = Bound::new(
            py,
            Pose {
                x: px,
                y: py_,
                z: pz,
                qw: pqw,
                qx: pqx,
                qy: pqy,
                qz: pqz,
                map_id: pmap,
            },
        )?
        .into_any()
        .unbind();

        let (vx, vy, om) = self.velocity.clone().unwrap_or((0.0, 0.0, 0.0));
        let velocity = Bound::new(py, Velocity { vx, vy, omega: om })?
            .into_any()
            .unbind();

        let ts = timestamp_ms.unwrap_or_else(|| {
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0)
        });

        Ok(Bound::new(
            py,
            ISO21423RobotState {
                version,
                manufacturer,
                serial_number,
                timestamp_ms: ts,
                operating_mode: self.mode.clone(),
                battery,
                pose,
                velocity,
                joint_positions: self.joint_positions.clone(),
                joint_torques: self.joint_torques.clone(),
                condition: self.condition.clone(),
                errors: self.errors.clone(),
            },
        )?
        .unbind())
    }
}

#[pymodule]
fn iso21423_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<BatteryState>()?;
    m.add_class::<Pose>()?;
    m.add_class::<Velocity>()?;
    m.add_class::<ISO21423RobotState>()?;
    m.add_class::<OperatingMode>()?;
    m.add_class::<Ros2StateAccumulator>()?;
    m.add_function(wrap_pyfunction!(yaw_to_quat, m)?)?;
    m.add_function(wrap_pyfunction!(telemetry_to_iso21423, m)?)?;
    m.add_function(wrap_pyfunction!(iso21423_to_telemetry, m)?)?;
    Ok(())
}
