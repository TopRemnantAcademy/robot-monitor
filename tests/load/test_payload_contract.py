"""Load-test payload contract test: field-by-field cross-check of
`tests/load/payload.js` <-> `simulator/mock_fleet.py`.

Why it is needed
----------------
Order 23's hard requirement is "the payload reuses mock_fleet's real 24-joint
structure". If this is verified only by eye, it is an empty promise -- exactly the
pitfall hit repeatedly in this repo (missing one of 24 joints, or deriving
max_joint_temp wrong, turns the load test into "testing the mqtt_dropped counter").

This file turns that requirement into an executable assertion:
  1. **Parse** the real joint names and payload field names from `simulator/mock_fleet.py`;
  2. **Parse** the same-name constants declared on the load-test side from `tests/load/payload.js`;
  3. Assert both sides are equal, and assert the sample message produced by payload.js
     passes the backend's Pydantic validation.

It runs without a pytest dependency: `python tests/load/test_payload_contract.py`.

WARNING: This file does **not** assert the numeric distribution (that is random);
it only asserts the "structural contract" -- a drifted structure must go red,
while a drifted value does not affect the verdict.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PAYLOAD_JS = ROOT / "tests" / "load" / "payload.js"
MOCK_FLEET = ROOT / "simulator" / "mock_fleet.py"
TELEMETRY = ROOT / "backend_ingestion" / "app" / "schemas" / "telemetry.py"


# ------------------------------------------------------------------ parse mock_fleet.py

def _mock_fleet_source() -> str:
    return MOCK_FLEET.read_text(encoding="utf-8")


def mock_fleet_joint_names() -> list[str]:
    """Extract the 24 joint key names from mock_fleet.py's `JOINT_NAMES: tuple[str, ...] = (...)`.

    Use ast rather than regex: regex is fragile for annotated assignments and multiline tuples.
    """
    tree = ast.parse(_mock_fleet_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == "JOINT_NAMES":
            values = ast.literal_eval(node.value)
            return list(values)
    raise AssertionError("JOINT_NAMES not found in mock_fleet.py")


def mock_fleet_payload_fields() -> list[str]:
    """Extract top-level field names from `RobotState.build_payload`'s return dict (preserving source order).

    build_payload's return is a dict literal, so ast can give the keys precisely.
    """
    tree = ast.parse(_mock_fleet_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_payload":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    keys = []
                    for k in sub.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.append(k.value)
                    if keys:
                        return keys
            raise AssertionError("build_payload's return is not a dict literal")
    raise AssertionError("build_payload not found in mock_fleet.py")


# ------------------------------------------------------------------ parse payload.js

def payload_js_source() -> str:
    return PAYLOAD_JS.read_text(encoding="utf-8")


def payload_js_joint_names() -> list[str]:
    """Extract the string array from payload.js's `export const JOINT_NAMES = [...]`. """
    src = payload_js_source()
    m = re.search(r"export const JOINT_NAMES = \[(.*?)\];", src, re.S)
    if not m:
        raise AssertionError("JOINT_NAMES not found in payload.js")
    return re.findall(r"'([^']+)'", m.group(1))


def node_build_sample() -> dict:
    """Actually execute payload.js with node to obtain a sample message.

    This step is "real execution" rather than "guessing from source": if payload.js's
    syntax/logic is broken, it fails here directly instead of passing by luck of the assertion.
    """
    script = (
        "import { Robot } from '" + PAYLOAD_JS.as_uri() + "';\n"
        "const r = new Robot(1); r.step(0.1);\n"
        "process.stdout.write(JSON.stringify(r.buildPayload(1700000000000)));\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError("node failed to execute payload.js:\n" + proc.stderr.strip())
    return json.loads(proc.stdout)


# ------------------------------------------------------------------ backend schema fields

def telemetry_schema_fields() -> list[str]:
    """Extract field names from Telemetry(BaseModel)'s class-body annotations."""
    tree = ast.parse(TELEMETRY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Telemetry":
            fields = []
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
    if fields:
        return fields
    raise AssertionError("Telemetry fields not found in telemetry.py")


# ------------------------------------------------------------------ assertions

def test_mock_fleet_exists() -> None:
    assert MOCK_FLEET.is_file(), f"Missing {MOCK_FLEET} (the simulator is the source of truth for payload)"
    assert PAYLOAD_JS.is_file(), f"Missing {PAYLOAD_JS}"


def test_joint_names_match_mock_fleet() -> None:
    """(1) The 24 joint key names and their order must match mock_fleet.py verbatim."""
    expected = mock_fleet_joint_names()
    actual = payload_js_joint_names()
    assert len(expected) == 24, f"mock_fleet JOINT_NAMES should be 24, got {len(expected)}"
    assert actual == expected, (
        "Joint key names differ from mock_fleet.py\n"
        f"  mock_fleet: {expected}\n  payload.js: {actual}\n"
        f"  only in mock_fleet: {sorted(set(expected) - set(actual))}\n"
        f"  only in payload.js: {sorted(set(actual) - set(expected))}"
    )


def test_payload_fields_match_mock_fleet() -> None:
    """(2) The top-level field set must be exactly the same as mock_fleet.build_payload."""
    expected = set(mock_fleet_payload_fields())
    sample = node_build_sample()
    actual = set(sample.keys())
    assert actual == expected, (
        "Payload top-level fields differ from mock_fleet.py\n"
        f"  only in mock_fleet: {sorted(expected - actual)}\n"
        f"  only in payload.js: {sorted(actual - expected)}"
    )


def test_sample_payload_passes_backend_schema_shape() -> None:
    """(3) The sample message must satisfy the backend Telemetry shape constraints (judgeable without pydantic)."""
    sample = node_build_sample()
    fields = set(telemetry_schema_fields())

    # Among fields declared by the backend, apart from these optional pose/time fields,
    # the load test must emit all of them.
    optional = {"battery_temp", "max_joint_temp", "pos_x", "pos_y", "pos_z",
                "orient_w", "orient_x", "orient_y", "orient_z", "ts"}
    required = fields - optional
    missing = required - set(sample.keys())
    assert not missing, f"Sample message missing required backend fields: {sorted(missing)}"

    # robot_id must match ^humanoid-[0-9]{3}$ (telemetry.py:27)
    assert re.fullmatch(r"humanoid-[0-9]{3}", sample["robot_id"]), (
        f"robot_id does not match ^humanoid-[0-9]{{3}}$: {sample['robot_id']!r}"
    )

    # The three joint-dict keys must be identical (telemetry.py:48-54) and each have 24 keys.
    names = set(payload_js_joint_names())
    for key in ("joint_temperatures", "joint_positions", "joint_torques"):
        got = set(sample[key].keys())
        assert got == names, f"{key} key set differs from spec/00 §5.5's 24 keys: {sorted(got ^ names)}"

    # max_joint_temp must equal the highest joint temperature (telemetry.py:57-62 precomputed contract)
    assert sample["max_joint_temp"] == max(sample["joint_temperatures"].values()), (
        "max_joint_temp != max(joint_temperatures)"
    )

    # battery_pct in [0, 100]
    assert 0.0 <= sample["battery_pct"] <= 100.0


def test_payload_size_in_expected_range() -> None:
    """(4) A single message's size should be in the same order as mock_fleet (~2 KB) to prevent sending a slimmed-down version."""
    size = len(json.dumps(node_build_sample(), separators=(",", ":")))
    assert 1500 <= size <= 3500, f"Single payload {size} B, outside mock_fleet's isomorphic range [1500, 3500]"


def main() -> int:
    checks = [
        ("payload.js / mock_fleet.py exist", test_mock_fleet_exists),
        ("24 joint key names and order match verbatim", test_joint_names_match_mock_fleet),
        ("top-level fields match mock_fleet", test_payload_fields_match_mock_fleet),
        ("sample message satisfies backend schema shape", test_sample_payload_passes_backend_schema_shape),
        ("message size in same order as mock_fleet", test_payload_size_in_expected_range),
    ]
    failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
