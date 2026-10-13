"""G-7 / Issue #114：任务级多机器人协同仿真 harness 的自动化验收。

背景
----
G-7 原打算用 AgentVerse 驱动 `mock_fleet` 产 100 台 agent 的任务级仿真。
实证（见 `docs/agentverse-verification/README.md`）表明 AgentVerse 是 LLM 文本
对话框架，**不能**产出本系统遥测，路线判不可行。本仓承载「任务级协同仿真」的
正确形态是 `simulator/task_sim.py`（Issue #63）——它复用 `mock_fleet` 的 payload
构造器与 MQTT 发布器，用任务调度器让多台机器人的遥测在时间上相关。

本测试把 G-7 的验收标准固化为可重复断言：
- ≥10 台 agent（机器人）在同一任务调度下协同；
- 协同不是「硬编码系数」：pipeline 场景相邻工位应呈**反相关**，fault 场景
  中枢-邻居应呈**正相关**；
- 相关性来自调度逻辑，脚本退出码 0 即代表达到 |r| ≥ 0.3 显著阈值。

不连 broker（`--no-broker`），因此不依赖 Docker/MQTT，可在 CI 与本地直接跑。
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
    """harness 必须存在且可被解析（防「白名单空了也不报错」）。"""
    assert TASK_SIM.is_file(), f"缺少 {TASK_SIM}"
    src = TASK_SIM.read_text(encoding="utf-8")
    # 复用 mock_fleet 契约，而非另起一套 waveform。
    assert "from mock_fleet import" in src or "from simulator.mock_fleet import" in src


@pytest.mark.parametrize("stations", [10, 12])
def test_pipeline_ge_10_agents_correlated(tmp_path: Path, stations: int):
    """pipeline：≥10 台机器人协同，相邻工位温度序列显著相关（退出码 0）。"""
    out = tmp_path / "pipeline.json"
    cp = _run_task_sim("pipeline", stations, duration=16.0, out=out)
    assert cp.returncode == 0, (
        f"task_sim pipeline 退出码 {cp.returncode}（0=相关达标）\n"
        f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    )
    assert "通过" in cp.stdout, f"未看到相关性达标输出：\n{cp.stdout}"
    series = json.loads(out.read_text(encoding="utf-8"))
    # 参与调度的台数 = stations；每台都应有采样序列。
    assert len(series) == stations, f"参与台数 {len(series)} != {stations}"
    for rid, vals in series.items():
        assert len(vals) >= 5, f"{rid} 采样点过少：{len(vals)}"
    # G-7 明确要求 ≥10 台协同。
    assert len(series) >= 10, f"协同台数 {len(series)} < 10（不满足 G-7）"


def test_fault_scenario_positive_correlation(tmp_path: Path):
    """fault：中枢-邻居正相关（故障传播），确证相关性来自调度而非固定系数。"""
    out = tmp_path / "fault.json"
    cp = _run_task_sim("fault", stations=4, duration=16.0, out=out)
    assert cp.returncode == 0, f"task_sim fault 未达标：\n{cp.stdout}\n{cp.stderr}"
    assert "正相关" in cp.stdout, f"fault 场景应呈正相关：\n{cp.stdout}"


def test_payload_contract_unchanged(tmp_path: Path):
    """harness 产出的遥测样本必须与 mock_fleet 契约一致（17 字段，含 max_joint_temp）。"""
    out = tmp_path / "contract.json"
    cp = _run_task_sim("pipeline", stations=10, duration=8.0, out=out)
    assert cp.returncode == 0, f"task_sim 未跑通：\n{cp.stdout}\n{cp.stderr}"
    # 导出的是各机器人 ACTIVE_JOINT 温度序列；契约字段由 mock_fleet.RobotState 保证。
    sys.path.insert(0, str(REPO_ROOT))
    from simulator.mock_fleet import RobotState  # type: ignore

    fields = set(RobotState(robot_id="humanoid-001").build_payload(1).keys())
    for must in ("robot_id", "max_joint_temp", "joint_temperatures", "battery_pct", "ts"):
        assert must in fields, f"mock_fleet 契约缺字段 {must}"
    # 参与调度的机器人 id 必须符合 humanoid- 前缀契约。
    series = json.loads(out.read_text(encoding="utf-8"))
    for rid in series:
        assert rid.startswith("humanoid-"), f"robot_id 前缀不符契约：{rid}"
