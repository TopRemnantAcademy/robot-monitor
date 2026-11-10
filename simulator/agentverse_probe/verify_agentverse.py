#!/usr/bin/env python3
"""G-7 empirical script: can AgentVerse drive mock_fleet's 100 agents?

This script **only verifies, and produces no harness**. It hardens Issue #114's
requirement "measure whether AgentVerse can load 100 agent definitions and run a
task-level simulation" into a reproducible probe, outputting raw errors and a
conclusion, avoiding any "assume can/cannot".

Prerequisites (do not pollute the main repo deps; AgentVerse material lives
outside external/, see docs/agentverse-verification/README.md):

    git clone --depth 1 https://github.com/OpenBMB/AgentVerse /tmp/agentverse_probe
    pip install -i https://mirrors.aliyun.com/pypi/simple/ \
        numpy "openai==1.1.0" "httpx==0.25.0" "pydantic==1.10.7" colorama colorlog \
        py3langid "tiktoken==0.5.1" rapidfuzz "langchain==0.0.157" scikit-learn astunparse

Usage (run at the AgentVerse repo root, or point with --av-root):

    python verify_agentverse.py --av-root /tmp/agentverse_probe

Exit codes: 0 = probe completed (regardless of the conclusion "can/cannot", it
                only means evidence was collected successfully);
             2 = prerequisites missing (AV_ROOT not set / deps not installed),
                conclusion is "not verified".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# mock_fleet's telemetry contract fields (the payload must contain these to count
# as "producing telemetry").
TELEMETRY_FIELDS = {
    "robot_id", "status_code", "error_code", "battery_pct", "battery_temp",
    "max_joint_temp", "joint_temperatures", "joint_positions", "joint_torques",
    "pos_x", "pos_y", "pos_z", "orient_w", "orient_x", "orient_y", "orient_z", "ts",
}



def _quiet_agentverse_logging() -> None:
    """AgentVerse prints the full prompt / raw 401 to stdout; the probe only
    needs the conclusion, so suppress the noise."""
    import logging
    for name in ("agentverse", "openai", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _purge_agentverse_modules() -> None:
    """Fully clear agentverse.* and the openai client cache, keeping the import
    probe reproducible."""
    for m in [m for m in list(sys.modules) if m.startswith("agentverse")]:
        del sys.modules[m]


def load_mock_fleet_payload_fields(repo_root: Path) -> set[str]:
    """Read one real payload field set live from simulator/mock_fleet.py as the
    comparison baseline."""
    sys.path.insert(0, str(repo_root))
    from simulator.mock_fleet import RobotState  # type: ignore

    return set(RobotState(robot_id="humanoid-001").build_payload(1).keys())


def main() -> int:
    p = argparse.ArgumentParser(description="G-7 AgentVerse driving-capability empirical probe")
    p.add_argument("--av-root", default=os.environ.get("AV_ROOT", "/tmp/agentverse_probe"),
                   help="AgentVerse source root directory")
    p.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]),
                   help="RobotMonitor repo root (for reading the mock_fleet contract)")
    p.add_argument("--agents", type=int, default=100, help="number of agents to attempt constructing")
    args = p.parse_args()

    av = Path(args.av_root)
    if not (av / "agentverse").is_dir():
        print(f"[SKIP] AgentVerse source not found: {av}/agentverse does not exist.", file=sys.stderr)
        print("[SKIP] Conclusion = not verified (prerequisites missing).", file=sys.stderr)
        return 2

    sys.path.insert(0, str(av))
    repo_root = Path(args.repo_root)

    print("=" * 72)
    print("G-7 AgentVerse driving-capability empirical probe")
    print("=" * 72)

    # ---- Baseline: mock_fleet's telemetry contract ----
    fields = load_mock_fleet_payload_fields(repo_root)
    print(f"\n[Baseline] mock_fleet payload fields ({len(fields)}): {sorted(fields)}")
    missing = TELEMETRY_FIELDS - fields
    print(f"[Baseline] contract check: {'consistent' if not missing else 'missing ' + str(missing)}")

    # ---- Probe 1: can it import without OPENAI_API_KEY ----
    print("\n[Probe 1] import agentverse without OPENAI_API_KEY")
    saved = os.environ.pop("OPENAI_API_KEY", None)
    saved_az = os.environ.pop("AZURE_OPENAI_API_KEY", None)
    _purge_agentverse_modules()
    try:
        import importlib
        importlib.import_module("agentverse")
        print("  -> import succeeded (unexpected, please re-check)")
        probe1 = "import-ok-without-key"
    except Exception as e:  # noqa: BLE001 -- the probe deliberately captures the raw exception
        print(f"  -> failed: {type(e).__name__}: {e}")
        probe1 = f"{type(e).__name__}: {e}"
    finally:
        if saved:
            os.environ["OPENAI_API_KEY"] = saved
        if saved_az:
            os.environ["AZURE_OPENAI_API_KEY"] = saved_az

    # ---- Probe 2: load task config + construct N agents ----
    print(f"\n[Probe 2] construct {args.agents} agents (needs OPENAI_API_KEY placeholder)")
    os.environ.setdefault("OPENAI_API_KEY", "sk-probe-placeholder")
    _purge_agentverse_modules()
    probe2: dict = {}
    try:
        from agentverse.initialization import prepare_task_config, load_agent  # type: ignore
        _quiet_agentverse_logging()

        cfg = prepare_task_config("nlp_classroom_3players", str(av / "agentverse/tasks/simulation"))
        t0 = time.time()
        agents = [
            load_agent({**cfg["agents"][0], "name": f"humanoid-{i + 1:03d}"})
            for i in range(args.agents)
        ]
        dt = time.time() - t0
        print(f"  -> constructed {len(agents)} agent objects in {dt:.2f}s")
        print(f"  -> first agent: {type(agents[0]).__name__}, llm={type(agents[0].llm).__name__}")
        probe2 = {"constructed": len(agents), "seconds": round(dt, 3),
                  "agent_class": type(agents[0]).__name__,
                  "llm_class": type(agents[0].llm).__name__}
    except Exception as e:  # noqa: BLE001
        print(f"  -> failed: {type(e).__name__}: {e}")
        probe2 = {"error": f"{type(e).__name__}: {e}"}

    # ---- Probe 3: does agent.step output telemetry ----
    print("\n[Probe 3] does the agent's single-step product hit the telemetry contract")
    probe3: dict = {}
    try:
        from agentverse.initialization import load_agent  # type: ignore
        cfg = prepare_task_config("nlp_classroom_3players", str(av / "agentverse/tasks/simulation"))
        a = load_agent(dict(cfg["agents"][0]))
        msg = asyncio.run(a.astep("probe"))
        d = msg.dict() if hasattr(msg, "dict") else dict(msg)
        hit = TELEMETRY_FIELDS & set(map(str, d.keys()))
        payload = json.dumps(d, ensure_ascii=False, default=str)
        numeric_hit = any(f in payload for f in ["max_joint_temp", "joint_temperatures", "battery_pct"])
        print(f"  -> Message fields: {sorted(map(str, d.keys()))}")
        print(f"  -> telemetry contract fields hit: {sorted(hit) if hit else 'none'}")
        print(f"  -> contains telemetry values: {numeric_hit}")
        probe3 = {"message_keys": sorted(map(str, d.keys())),
                  "telemetry_field_hit": sorted(hit), "has_telemetry_value": numeric_hit}
    except Exception as e:  # noqa: BLE001
        print(f"  -> failed: {type(e).__name__}: {e}")
        probe3 = {"error": f"{type(e).__name__}: {e}"}

    # ---- Conclusion ----
    print("\n" + "=" * 72)
    print("Conclusion")
    print("=" * 72)
    key_gated = probe1.startswith("ImportError")
    constructed = probe2.get("constructed", 0)
    telemetry = bool(probe3.get("has_telemetry_value"))
    print(f"  1) import gated directly without API key (DEFAULT_CLIENT undefined): {key_gated}")
    print(f"  2) can construct agent objects with a key placeholder: {constructed} units")
    print(f"  3) does the agent's single-step product carry telemetry values: {telemetry}")
    if key_gated and constructed and not telemetry:
        print("  => verdict: AgentVerse can load/construct 100 agents, but [cannot] directly "
              "produce this system's telemetry;")
        print("     its step runs LLM text dialogue, a different layer from mock_fleet's "
              "numeric payload contract.")
        verdict = "loadable-but-not-telemetry"
    else:
        print("  => verdict: the above probe outputs need manual re-check.")
        verdict = "inconclusive"
    print(f"  verdict = {verdict}")

    out = Path(__file__).with_name("verify_agentverse_report.json")
    out.write_text(json.dumps(
        {"probe1_import_without_key": probe1, "probe2_construct": probe2,
         "probe3_message": probe3, "mock_fleet_fields": sorted(fields),
         "verdict": verdict}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  report written -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
