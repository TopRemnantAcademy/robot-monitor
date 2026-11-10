"""Guard: the ISO 21423 envelope exposes a backend-agnostic ``to_dict()``.

``simulator/iso21423_emitter.py`` relied on ``dataclasses.asdict(envelope)``.
Once the envelope is the compiled ``iso21423_rs`` pyclass (not a dataclass),
that call raises ``TypeError``. Both backends now expose ``to_dict()``; this test
fails loudly if either backend drops it or returns a non-dict / wrong shape.
"""

import dataclasses
import importlib

import bridge.iso21423 as iso
import bridge._iso21423_py as ref
import pytest


_BACKEND = getattr(iso, "_BACKEND", "python")

# A representative telemetry payload (canonical key names per _iso21423_py).
_PAYLOAD = {
    "status_code": 1,
    "error_code": 0,
    "robot_id": "humanoid-001",
    "ts": 1700000000000,
    "battery_pct": 87.5,
    "battery_voltage": 24.1,
    "battery_current": -1.2,
    "battery_temp": 31.0,
    "battery_status": "discharging",
    "pos_x": 1.2,
    "pos_y": -3.4,
    "pos_z": 0.0,
    "orient_w": 1.0,
    "orient_x": 0.0,
    "orient_y": 0.0,
    "orient_z": 0.0,
    "vx": 0.1,
    "vy": -0.05,
    "omega": 0.02,
    "map_id": "floor1",
    "joint_positions": {"j0": 0.1, "j1": 0.2},
    "joint_torques": {"j0": 0.3},
    "joint_temperatures": {"j0": 35.0},
}


def _build_via(mod):
    env = mod.telemetry_to_iso21423(_PAYLOAD, manufacturer="robotmonitor-sim")
    return env, mod.iso21423_to_telemetry(env)


def test_envelope_has_to_dict():
    env, _ = _build_via(iso)
    assert hasattr(env, "to_dict"), "envelope must expose to_dict()"


def test_to_dict_returns_serializable_dict():
    env, _ = _build_via(iso)
    d = env.to_dict()  # must not raise TypeError (the old dataclasses.asdict trap)
    assert isinstance(d, dict)
    assert d["serial_number"] == "humanoid-001"
    assert isinstance(d["battery"], dict)
    assert isinstance(d["pose"], dict)
    assert isinstance(d["velocity"], dict)
    # JSON-serializable (what the simulator logs / publishes).
    import json

    json.dumps(d)


def test_to_dict_matches_python_reference_structure():
    _, _ = _build_via(iso)
    env_ref, _ = _build_via(ref)
    got = iso.iso21423_to_telemetry(iso.telemetry_to_iso21423(_PAYLOAD,
                                                              manufacturer="robotmonitor-sim"))
    # The flattened telemetry mapper is the real contract; to_dict is just a
    # nested view. Ensure both backends produce the identical flattened telemetry.
    got_ref = ref.iso21423_to_telemetry(ref.telemetry_to_iso21423(_PAYLOAD,
                                                                  manufacturer="robotmonitor-sim"))
    assert got == got_ref, "flattened telemetry must match the pure-Python reference"
    # And the nested view must be structurally identical too.
    nested = iso.telemetry_to_iso21423(_PAYLOAD, manufacturer="robotmonitor-sim").to_dict()
    nested_ref = dataclasses.asdict(
        ref.telemetry_to_iso21423(_PAYLOAD, manufacturer="robotmonitor-sim")
    )
    assert nested.keys() == nested_ref.keys()
    assert nested["serial_number"] == nested_ref["serial_number"]
    assert nested["battery"].keys() == nested_ref["battery"].keys()
