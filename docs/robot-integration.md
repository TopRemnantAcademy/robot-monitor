# Robot Integration — ROS 2 & ISO 21423:2026 Bridge

RobotMonitor started life as a **simulator-only** monitoring stack (100
`humanoid-NNN` robots emitted by `simulator/mock_fleet.py`). The competitive
analysis ([`COMPETITIVE-ANALYSIS.md`](./COMPETITIVE-ANALYSIS.md), item **A**) named
"no real-robot connection / no interoperability standard" as the single biggest
gap versus OpenRobOps (ISO 21423) and the ROS 2 humanoid crowd.

This document describes the bridge that closes that gap: a real-robot ingestion
path that maps **ROS 2** (via rosbridge) and the **ISO 21423:2026** envelope into
RobotMonitor's existing telemetry ingest topic, with **zero changes to
`backend_ingestion`**.

---

## 1. What ISO 21423:2026 actually is (grounded)

- **Origin:** ISO/TC 299; published **2026-10**; announced by the Association for
  Advancing Automation (A3).
- **Scope:** common communication requirements for **industrial mobile robots
  (IMRs)**, fleet managers and connected systems. It standardises the exchange
  of **location, speed, direction, operating condition, tasking and availability**.
- **Basis:** builds directly on **VDA 5050** (MQTT + JSON AGV interface) and the
  **MassRobotics AMR interoperability standard**. Explicitly *not* a safety or
  navigation standard.
- **Wire model:** JSON messages over a publish/subscribe bus (MQTT-class) — exactly
  the model RobotMonitor already uses, and exactly what **rosbridge** exposes for
  ROS 2.

> **Conformance note.** The normative field set is behind the paywalled standard
> text. The envelope in `bridge/iso21423.py` implements the *publicly documented
> common elements* and is structured so adding normative fields later is purely
> additive. It is **not** a claim of certified conformance — that requires the
> published text + a conformance suite.

---

## 2. Architecture

```
                         REAL ROBOT (ROS 2)                 SIMULATOR
                  ┌──────────────────────────┐        ┌─────────────────────┐
                  │ ros2_control / drivers    │        │ mock_fleet /        │
                  │ + rosbridge_suite         │        │ iso21423_emitter    │
                  └────────────┬─────────────┘        └──────────┬──────────┘
                               │ WebSocket (JSON)                 │ in-process
                               │ /battery_state                    │ envelope build
                               │ /joint_states                     │
                               │ /odom                             │
                               ▼                                  │
                  ┌──────────────────────────┐                     │
                  │  bridge/ros2_bridge.py    │                     │
                  │   ROS2StateAccumulator    │                     │
                  │   (pure ROS2 -> envelope) │                     │
                  └────────────┬─────────────┘                     │
                               │  ISO21423RobotState               │
                               │  (bridge/iso21423.py)             │
                               └────────────┬──────────────────────┘
                                            │  iso21423_to_telemetry()
                                            ▼
                                  RobotMonitor telemetry dict
                                            │
                                            │  publish  (bridge.publish_telemetry)
                                            ▼
                                  MQTT  robots/{id}/telemetry   ◀── SAME topic mock_fleet uses
                                            │
                                            ▼
                                  backend_ingestion (UNCHANGED)
                                  TimescaleDB + Redis + alert_engine
```

**Key property:** real robots and the simulator converge on the **same ingest
topic** (`robots/{id}/telemetry`). `backend_ingestion` is untouched — the bridge
is a pure producer-side adapter.

---

## 3. ISO 21423:2026 envelope → RobotMonitor telemetry mapping

`bridge/iso21423.py` defines `ISO21423RobotState` and two lossless mappers:
`telemetry_to_iso21423()` and `iso21423_to_telemetry()` (round-trip asserted by
`bridge/tests/test_iso21423.py`).

| ISO 21423 field | Type | RobotMonitor telemetry field |
| :--- | :--- | :--- |
| `serial_number` | str | `robot_id` |
| `operating_mode` | enum IDLE/AUTOMATIC/MANUAL/SERVICE/ERROR | `status_code` (1/1/1/2/3) + `error_code` |
| `errors[]` (non-empty) | list[str] | forces `status_code=3` |
| `battery.charge_pct` | float (%) | `battery_pct` |
| `battery.temperature_c` | float | `battery_temp` |
| `condition{}` (operating-condition readings) | dict | `joint_temperatures{}` + `max_joint_temp` |
| `joint_positions{}` | dict | `joint_positions{}` |
| `joint_torques{}` | dict | `joint_torques{}` |
| `pose.{x,y,z}` | float | `pos_x, pos_y, pos_z` |
| `pose.{qw,qx,qy,qz}` | float (quaternion) | `orient_w/x/y/z` |
| `timestamp_ms` | int (ms) | `ts` |

Velocity (`vx, vy, omega`) and `manufacturer`/`version` are carried in the
envelope for ISO 21423 fidelity but are not part of the legacy telemetry schema
(RobotMonitor is sampled at 10 Hz; velocity is derivable downstream).

---

## 4. ROS 2 topic → envelope mapping

`bridge/ros2_bridge.py` (`ROS2StateAccumulator`) maps the standard ROS 2 message
types rosbridge delivers:

| ROS 2 topic | Message type | Mapped to |
| :--- | :--- | :--- |
| `/battery_state` | `sensor_msgs/msg/BatteryState` | `battery.*` (0..1 fraction → percent) |
| `/joint_states` | `sensor_msgs/msg/JointState` | `joint_positions{}`, `joint_torques{}` |
| `/odom` | `nav_msgs/msg/Odometry` | `pose.*`, `velocity.*` |
| `/robot_pose` (alt) | `geometry_msgs/msg/PoseStamped` | `pose.*` |
| `/robot_mode` | `std_msgs/msg/String` | `operating_mode` |

Joint **temperatures** are vendor-specific (not in `JointState`); a vendor
thermal topic should populate `condition{}`. This is documented as a known
extension point, not a gap in the standard mapping.

---

## 5. ⚠️ Known limitation before real robots can be ingested

`backend_ingestion/app/schemas/telemetry.py` constrains
`ROBOT_ID_PATTERN = r"^humanoid-[0-9]{3}$"`. A physical robot's
`serial_number` (e.g. `agv-042`, `UR-10e-7`) **will be rejected** by the
existing `Telemetry` validator.

This milestone deliberately does **not** relax that pattern (it is part of the
OpenAPI contract gate and the 30k e2e suite, which must stay green). The bridge
therefore:

- **preserves** the real `serial_number` as `robot_id` (faithful, correct output);
- for the **simulator** emitter, reuses `humanoid-NNN` so the demo flows through
  the unchanged pipeline.

**Required follow-up to fully "unlock" real robots:** generalize
`ROBOT_ID_PATTERN` (e.g. `^[\w-]{2,32}$`) and re-baseline
`docs/contracts/openapi.snapshot.json` + the e2e fixtures. Tracked as the
remaining sub-task of competitive item **A**.

---

## 6. Running it

### 6.1 Simulator in ISO 21423 emit mode (proves the standard path)
```bash
pip install -r simulator/requirements.txt -r bridge/requirements.txt
python simulator/iso21423_emitter.py --robots 5 --broker localhost --port 1883 --duration 10
```
Each sample is wrapped in an ISO 21423 envelope (logged as JSON) and republished
as RobotMonitor telemetry on `robots/{id}/telemetry`.

### 6.2 A real ROS 2 robot (rosbridge → MQTT)
```bash
# On the robot: ros2 launch rosbridge_server rosbridge_websocket.launch.xml
export ROSBRIDGE_URL=ws://<robot-ip>:9090
export ROBOT_MANUFACTURER=my-robot-co
export ROBOT_SERIAL=agv-042
export MQTT_BROKER=localhost MQTT_PORT=1883
pip install -r bridge/requirements.txt aiomqtt
python -m bridge.ros2_bridge        # relays envelope -> robots/{id}/telemetry
```

### 6.3 Unit tests (no ROS, no broker)
```bash
pip install -r bridge/requirements.txt pytest pytest-timeout
python -m pytest bridge/tests/ -v --timeout=60
```

---

## 7. CI

A dedicated **`bridge`** job runs `bridge/tests/` (standalone, no `app`-package
collision) on GitHub Actions and CNB, plus a `compileall` / test-file-sanity
gate. See `.github/workflows/ci.yml` and `.cnb.yml`.
