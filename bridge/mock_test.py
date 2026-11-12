"""Comprehensive mock test: Rust vs Python ROS2 -> ISO 21423 parity.

Drives a battery of *mock* ROS 2 messages (as rosbridge would deliver them:
plain JSON) through BOTH the compiled Rust ``Ros2StateAccumulator`` and the
pure-Python ``_ROS2StateAccumulatorPy`` reference, then:

  1. builds the ISO 21423 envelope (``to_dict``) and asserts field-exact parity,
  2. converts to the RobotMonitor telemetry wire dict (``iso21423_to_telemetry``)
     and asserts field-exact parity on the ACTUAL emitted format,
  3. checks per-scenario invariants on the production (Rust) output.

Timestamp fields (``timestamp_ms`` / ``ts``) are wall-clock by nature and are
excluded from parity -- they are not part of the ROS -> ISO mapping.

Run (from repo root, with the bridge venv active):
    cd robot-monitor
    bridge/.venv/Scripts/activate
    python bridge/mock_test.py
"""
from __future__ import annotations

from bridge.ros2_bridge import ROS2StateAccumulator, _ROS2StateAccumulatorPy
from bridge.iso21423 import _BACKEND as ENVELOPE_BACKEND
from bridge.ros2_bridge import _ACC_BACKEND
from iso21423_rs import iso21423_to_telemetry as to_tel_rust
from bridge._iso21423_py import iso21423_to_telemetry as to_tel_py

TS_IGNORE = {"timestamp_ms", "ts"}


def flat(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(flat(v, prefix + k + "."))
            elif isinstance(v, list):
                out[prefix + k] = tuple(v)
            elif isinstance(v, tuple):
                out[prefix + k] = v
            else:
                out[prefix + k] = v
    return out


def diff(a, b, ignore=TS_IGNORE):
    fa, fb = flat(a), flat(b)
    allk = sorted(set(fa) | set(fb))
    out = []
    for k in allk:
        if k in ignore:
            continue
        va, vb = fa.get(k), fb.get(k)
        if va != vb:
            out.append((k, va, vb))
    return out


# (name, [ (msg_type, msg), ... ], [ (target, expected), ... ])
# target: "env.<flat.path>"  or  "tel.<telemetry.key>"
SCENARIOS = [
    ("empty_defaults", [],
     [("env.battery.charge_pct", 0.0), ("env.pose.x", 0.0), ("env.pose.qw", 1.0),
      ("env.velocity.vx", 0.0), ("env.operating_mode", "AUTOMATIC"),
      ("env.joint_positions", {}), ("env.errors", [])]),

    ("battery_fraction", [
        ("sensor_msgs/msg/BatteryState",
         {"percentage": 0.82, "voltage": 24.1, "current": -1.5,
          "temperature": 30.0, "power_supply_status": 2})],
     [("env.battery.charge_pct", 82.0), ("env.battery.voltage_v", 24.1),
      ("env.battery.current_a", -1.5), ("env.battery.temperature_c", 30.0),
      ("env.battery.power_supply_status", 2)]),

    ("battery_already_pct", [
        ("sensor_msgs/msg/BatteryState", {"percentage": 90.0})],
     [("env.battery.charge_pct", 90.0)]),  # >1 treated as already a percentage

    ("battery_missing_pct", [
        ("sensor_msgs/msg/BatteryState", {"voltage": 12.0})],
     [("env.battery.charge_pct", 0.0)]),

    ("joint_full_mismatch", [
        ("sensor_msgs/msg/JointState",
         {"name": ["a", "b", "c"], "position": [0.1, 0.2], "effort": [1.0]})],
     [("env.joint_positions", {"a": 0.1, "b": 0.2}),  # c dropped: no position
      ("env.joint_torques", {"a": 1.0})]),              # only name[0] has effort

    ("joint_partial", [
        ("sensor_msgs/msg/JointState",
         {"name": ["x", "y"], "position": [1.0, 2.0]})],
     [("env.joint_positions", {"x": 1.0, "y": 2.0}), ("env.joint_torques", {})]),

    ("odom_full", [
        ("nav_msgs/msg/Odometry", {
            "pose": {"pose": {"position": {"x": 1.5, "y": 2.5, "z": 0.0},
                              "orientation": {"w": 0.999, "x": 0.0, "y": 0.0, "z": 0.044}}},
            "twist": {"twist": {"linear": {"x": 0.3, "y": 0.0, "z": 0.0},
                                "angular": {"x": 0.0, "y": 0.0, "z": 0.05}}}})],
     [("env.pose.x", 1.5), ("env.pose.y", 2.5), ("env.pose.z", 0.0),
      ("env.pose.qw", 0.999), ("env.pose.qz", 0.044),
      ("env.velocity.vx", 0.3), ("env.velocity.omega", 0.05)]),

    ("odom_no_twist", [
        ("nav_msgs/msg/Odometry",
         {"pose": {"pose": {"position": {"x": 5.0, "y": 6.0}}}})],
     [("env.pose.x", 5.0), ("env.pose.y", 6.0),
      ("env.velocity.vx", 0.0), ("env.velocity.omega", 0.0)]),

    ("odom_missing_angular", [
        ("nav_msgs/msg/Odometry",
         {"pose": {"pose": {"position": {"x": 1.0, "y": 1.0}}},
          "twist": {"twist": {"linear": {"x": 2.0}}}})],
     [("env.velocity.vx", 2.0), ("env.velocity.omega", 0.0)]),

    ("pose_stamped_flat", [
        ("geometry_msgs/msg/PoseStamped",
         {"pose": {"position": {"x": 7.0, "y": 8.0, "z": 1.0},
                   "orientation": {"w": 0.5, "x": 0.0, "y": 0.0, "z": 0.866}}})],
     [("env.pose.x", 7.0), ("env.pose.y", 8.0), ("env.pose.z", 1.0),
      ("env.pose.qw", 0.5), ("env.pose.qz", 0.866)]),

    ("pose_with_cov_stamped", [
        ("geometry_msgs/msg/PoseWithCovarianceStamped",
         {"pose": {"position": {"x": 9.0, "y": 10.0}}})],
     [("env.pose.x", 9.0), ("env.pose.y", 10.0)]),

    ("string_mode_automatic", [
        ("std_msgs/msg/String", {"data": "automatic"})],
     [("env.operating_mode", "AUTOMATIC")]),

    ("string_mode_manual", [
        ("std_msgs/msg/String", {"data": "MANUAL"})],
     [("env.operating_mode", "MANUAL")]),

    ("string_mode_error", [
        ("std_msgs/msg/String", {"data": "error"})],
     [("env.operating_mode", "ERROR")]),

    ("string_mode_unknown", [
        ("std_msgs/msg/String", {"data": "teleop_xyz"})],
     [("env.operating_mode", "AUTOMATIC")]),  # normalize falls back

    ("string_mode_empty", [
        ("std_msgs/msg/String", {"data": ""})],
     [("env.operating_mode", "AUTOMATIC")]),

    ("unknown_msg_type", [
        ("foo_msgs/msg/Bar", {"x": 1})],
     [("env.battery.charge_pct", 0.0), ("env.pose.x", 0.0)]),  # ignored

    ("multi_overwrite", [
        ("sensor_msgs/msg/BatteryState", {"percentage": 0.5}),
        ("sensor_msgs/msg/BatteryState", {"percentage": 0.82})],
     [("env.battery.charge_pct", 82.0)]),  # last write wins

    ("full_fleet_combo", [
        ("sensor_msgs/msg/BatteryState", {"percentage": 0.82, "voltage": 24.1}),
        ("sensor_msgs/msg/JointState",
         {"name": ["a", "b"], "position": [0.1, 0.2], "effort": [1.2, 1.3]}),
        ("nav_msgs/msg/Odometry",
         {"pose": {"pose": {"position": {"x": 1.5, "y": 2.5},
                           "orientation": {"w": 0.999, "z": 0.044}}},
          "twist": {"twist": {"linear": {"x": 0.3}, "angular": {"z": 0.05}}}}),
        ("geometry_msgs/msg/PoseStamped",
         {"pose": {"position": {"x": 7.0, "y": 8.0}}}),  # overwrites odom pose
        ("std_msgs/msg/String", {"data": "MANUAL"})],
     [("env.battery.charge_pct", 82.0),
      ("env.joint_positions", {"a": 0.1, "b": 0.2}),
      ("env.joint_torques", {"a": 1.2, "b": 1.3}),
      ("env.pose.x", 7.0), ("env.pose.y", 8.0),  # pose_stamped wins
      ("env.velocity.vx", 0.3), ("env.operating_mode", "MANUAL")]),

    ("error_status_telemetry", [
        ("std_msgs/msg/String", {"data": "ERROR"})],
     [("env.operating_mode", "ERROR"),
      ("tel.status_code", 3), ("tel.error_code", 0)]),  # no errors -> error_code 0
]


def resolve(d, path):
    """Navigate a dotted path on a (possibly nested) dict; None if absent."""
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def tolerant_eq(a, b):
    """Equality that treats list == tuple of equal contents as equal."""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return list(a) == list(b)
    return a == b


def _lookup(env_d, tel_d, target):
    if target.startswith("tel."):
        return resolve(tel_d, target[4:])
    if target.startswith("env."):
        return resolve(env_d, target[4:])
    return resolve(env_d, target)


def run():
    print(f"Backends: accumulator={_ACC_BACKEND}  envelope={ENVELOPE_BACKEND}")
    print(f"Scenarios: {len(SCENARIOS)}\n")
    total = len(SCENARIOS)
    passed = 0
    failed = 0
    for name, ingests, invariants in SCENARIOS:
        r_acc = ROS2StateAccumulator()
        p_acc = _ROS2StateAccumulatorPy()
        for t, m in ingests:
            r_acc.ingest(t, m)
            p_acc.ingest(t, m)

        r_env = r_acc.build(serial_number="R-TEST", manufacturer="acme")
        p_env = p_acc.build(serial_number="R-TEST", manufacturer="acme")
        r_env_d = r_env.to_dict()
        p_env_d = p_env.to_dict()
        env_diffs = diff(r_env_d, p_env_d)

        tel_problem = None
        tel_diffs = []
        try:
            r_tel = to_tel_rust(r_env)
            p_tel = to_tel_py(p_env)
            tel_diffs = diff(r_tel, p_tel)
        except Exception as e:  # noqa: BLE001
            tel_problem = f"{type(e).__name__}: {e}"

        r_tel_d = r_tel if tel_problem is None else {}
        inv_fails = []
        for target, expected in invariants:
            got = _lookup(r_env_d, r_tel_d, target)
            if not tolerant_eq(got, expected):
                inv_fails.append((target, expected, got))

        ok = (not env_diffs) and (not tel_diffs) and (not inv_fails) and (tel_problem is None)
        if ok:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}")
            for k, va, vb in env_diffs:
                print(f"        envelope DIFF {k}: rust={va!r} py={vb!r}")
            for k, va, vb in tel_diffs:
                print(f"        telemetry DIFF {k}: rust={va!r} py={vb!r}")
            if tel_problem:
                print(f"        telemetry ERROR: {tel_problem}")
            for tgt, exp, got in inv_fails:
                print(f"        invariant {tgt}: expected={exp!r} got={got!r}")

    print(f"\n=== RESULT: {passed}/{total} scenarios passed, {failed} failed ===")
    return failed == 0


# --------------------------------------------------------------------------
# R-VLA-EVAL Phase 1: policy_action_to_wire parity (Rust vs Python).
#
# Each scenario is a ROS 2 `robotmonitor_msgs/msg/PolicyAction` dict (as
# rosbridge would deliver it) plus the robot_id the bridge would parse from the
# MQTT topic. We run BOTH the compiled Rust `policy_action_to_wire` and the
# pure-Python reference and assert field-exact parity on the actual emitted
# wire dict, INCLUDING `ts` (it is derived deterministically from
# header.stamp, so it is safe to compare -- unlike the wall-clock timestamps in
# the telemetry path).
# --------------------------------------------------------------------------
ACTION_SCENARIOS = [
    # name, ros2 PolicyAction dict, robot_id, [ (field, expected), ... ]
    ("action_basic", {
        "policy_version": "vla-sim-0.1",
        "trial_id": "",
        "action": [0.1, -0.2, 0.3, -0.4, 0.5, -0.6],
        "goal": "",
        "header": {"stamp": {"sec": 1700000000, "nanosec": 500000000}},
    }, "humanoid-001", [
        ("robot_id", "humanoid-001"),
        ("policy_version", "vla-sim-0.1"),
        ("trial_id", None),
        ("action", [0.1, -0.2, 0.3, -0.4, 0.5, -0.6]),
        ("goal", None),
        ("ts", 1700000000500),
    ]),

    ("action_trial_and_goal", {
        "policy_version": "gr00t-n1.7",
        "trial_id": "trial-042",
        "action": [1.0, 2.0, 3.0],
        "goal": "pick the red cube",
        "header": {"stamp": {"sec": 1700001234, "nanosec": 0}},
    }, "humanoid-007", [
        ("robot_id", "humanoid-007"),
        ("policy_version", "gr00t-n1.7"),
        ("trial_id", "trial-042"),
        ("action", [1.0, 2.0, 3.0]),
        ("goal", "pick the red cube"),
        ("ts", 1700001234000),
    ]),

    ("action_no_header", {
        "policy_version": "psi0",
        "trial_id": "",
        "action": [0.0, 0.0, 0.0, 0.0],
        "goal": "",
    }, "humanoid-010", [
        ("robot_id", "humanoid-010"),
        ("policy_version", "psi0"),
        ("trial_id", None),
        ("action", [0.0, 0.0, 0.0, 0.0]),
        ("goal", None),
        ("ts", None),
    ]),

    ("action_missing_fields", {
        "action": [0.5],
    }, "humanoid-099", [
        ("robot_id", "humanoid-099"),
        ("policy_version", ""),       # missing -> ""
        ("trial_id", None),
        ("action", [0.5]),
        ("goal", None),
        ("ts", None),
    ]),

    ("action_empty_vector", {
        "policy_version": "vla-sim-0.2",
        "trial_id": "",
        "action": [],
        "goal": "",
        "header": {"stamp": {"sec": 1, "nanosec": 250000000}},
    }, "humanoid-050", [
        ("robot_id", "humanoid-050"),
        ("policy_version", "vla-sim-0.2"),
        ("trial_id", None),
        ("action", []),
        ("goal", None),
        ("ts", 1250),
    ]),
]


def run_action():
    from bridge._iso21423_py import policy_action_to_wire as pa_py
    try:
        from iso21423_rs import policy_action_to_wire as pa_rust
        rust_available = True
    except ImportError:
        pa_rust = None
        rust_available = False

    print("\n--- policy_action_to_wire parity "
          f"(rust={'yes' if rust_available else 'NO - rebuild iso21423_rs'}) ---")
    total = len(ACTION_SCENARIOS)
    passed = failed = 0

    if not rust_available:
        # Cannot assert Rust/Python zero divergence without the rebuilt extension.
        # Smoke-test the pure-Python reference for shape correctness only.
        for name, pa, robot_id, invariants in ACTION_SCENARIOS:
            out = pa_py(pa, robot_id=robot_id)
            for k, exp in invariants:
                if not tolerant_eq(resolve(out, k), exp):
                    failed += 1
                    print(f"  [PY-FAIL] {name}.{k}: expected={exp!r} "
                          f"got={resolve(out, k)!r}")
        print("[SKIP] Rust parity not asserted (extension lacks the function); "
              "rebuild with `maturin develop`.")
        # Shape check failure is a real failure; absence of the Rust fn is not.
        return failed == 0

    for name, pa, robot_id, invariants in ACTION_SCENARIOS:
        r_out = pa_rust(pa, robot_id=robot_id)
        p_out = pa_py(pa, robot_id=robot_id)
        # Compare EVERYTHING incl. ts (deterministic from header.stamp).
        field_diffs = diff(r_out, p_out, ignore=set())
        inv_fails = []
        for k, exp in invariants:
            if not tolerant_eq(resolve(r_out, k), exp):
                inv_fails.append((k, exp, resolve(r_out, k)))
        ok = (not field_diffs) and (not inv_fails)
        if ok:
            passed += 1
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}")
            for k, va, vb in field_diffs:
                print(f"        DIFF {k}: rust={va!r} py={vb!r}")
            for k, exp, got in inv_fails:
                print(f"        invariant {k}: expected={exp!r} got={got!r}")

    print(f"=== ACTION RESULT: {passed}/{total} passed, {failed} failed ===")
    return failed == 0


if __name__ == "__main__":
    import sys
    ok_tel = run()
    ok_act = run_action()
    sys.exit(0 if (ok_tel and ok_act) else 1)
