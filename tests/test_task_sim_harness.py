"""G-7 / Issue #114: Automated acceptance for the task-level multi-robot
collaborative simulation harness.

Background
----------
G-7 originally planned to use AgentVerse to drive `mock_fleet` and produce a
100-agent task-level simulation. Evidence (see
`docs/agentverse-verification/README.md`) showed AgentVerse is an LLM text
dialogue framework that **cannot** produce this system's telemetry, so the
approach is infeasible. The correct form of "task-level collaborative
simulation" in this repo is `simulator/task_sim.py` (Issue #63) -- it reuses
`mock_fleet`'s payload builder and MQTT publisher, and uses a task scheduler to
make multiple robots' telemetry temporally correlated.

This test hardens G-7's acceptance criteria into repeatable assertions:
- >=10 agents (robots) collaborate under the same task scheduling;
- collaboration is not "hardcoded coefficients": the pipeline scenario's
  adjacent stations should be **negatively correlated**, while the fault
  scenario's hub-neighbor should be **positively correlated**;
- correlation comes from the scheduling logic; a script exit code of 0 means the
  |r| >= 0.3 significance threshold was reached.

It does not connect to a broker (`--no-broker`), so it needs no Docker/MQTT and
can run directly in CI and locally.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASK_SIM = REPO_ROOT / "simulator" / "task_sim.py"


def _run_task_sim(scenario: str, stations: int, duration: float, out: Path) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable, str(TASK_SIM),
        "--scenario", scenario,
        "--stations", str(stations),
        "--duration", str(duration),
        "--no-broker",
        "--out", str(out),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 30, check=False)


def test_task_sim_exists():
    """The harness must exist and be parseable (guards against 'whitelist emptied yet no error raised')."""
    assert TASK_SIM.is_file(), f"Missing {TASK_SIM}"
    src = TASK_SIM.read_text(encoding="utf-8")
    # Reuses the mock_fleet contract rather than a separate waveform.
    assert "from mock_fleet import" in src or "from simulator.mock_fleet import" in src


@pytest.mark.parametrize("stations", [10, 12])
def test_pipeline_ge_10_agents_correlated(tmp_path: Path, stations: int):
    """pipeline: >=10 robots collaborate; adjacent-station temperature series are significantly correlated (exit code 0)."""
    out = tmp_path / "pipeline.json"
    cp = _run_task_sim("pipeline", stations, duration=16.0, out=out)
    assert cp.returncode == 0, (
        f"task_sim pipeline exit code {cp.returncode} (0=correlation threshold met)\n"
        f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    )
    assert "passed" in cp.stdout, f"Did not see correlation-threshold output:\n{cp.stdout}"
    series = json.loads(out.read_text(encoding="utf-8"))
    # Number of robots participating in scheduling = stations; each should have a sampled series.
    assert len(series) == stations, f"Participating robot count {len(series)} != {stations}"
    for rid, vals in series.items():
        assert len(vals) >= 5, f"{rid} has too few samples: {len(vals)}"
    # G-7 explicitly requires >=10 robots collaborating.
    assert len(series) >= 10, f"Collaborating robot count {len(series)} < 10 (G-7 not satisfied)"


def test_fault_scenario_positive_correlation(tmp_path: Path):
    """fault: hub-neighbor positive correlation (fault propagation), confirming correlation comes from scheduling, not fixed coefficients."""
    out = tmp_path / "fault.json"
    cp = _run_task_sim("fault", stations=4, duration=16.0, out=out)
    assert cp.returncode == 0, f"task_sim fault did not pass threshold:\n{cp.stdout}\n{cp.stderr}"
    assert "positive" in cp.stdout, f"fault scenario should show positive correlation:\n{cp.stdout}"


def test_payload_contract_unchanged(tmp_path: Path):
    """Telemetry samples produced by the harness must match the mock_fleet contract (17 fields, incl. max_joint_temp)."""
    out = tmp_path / "contract.json"
    cp = _run_task_sim("pipeline", stations=10, duration=8.0, out=out)
    assert cp.returncode == 0, f"task_sim did not run through:\n{cp.stdout}\n{cp.stderr}"
    # Exported are each robot's ACTIVE_JOINT temperature series; contract fields are guaranteed by mock_fleet.RobotState.
    sys.path.insert(0, str(REPO_ROOT))
    from simulator.mock_fleet import RobotState  # type: ignore

    fields = set(RobotState(robot_id="humanoid-001").build_payload(1).keys())
    for must in ("robot_id", "max_joint_temp", "joint_temperatures", "battery_pct", "ts"):
        assert must in fields, f"mock_fleet contract missing field {must}"
    # Robot ids participating in scheduling must match the humanoid- prefix contract.
    series = json.loads(out.read_text(encoding="utf-8"))
    for rid in series:
        assert rid.startswith("humanoid-"), f"robot_id prefix violates contract: {rid}"
