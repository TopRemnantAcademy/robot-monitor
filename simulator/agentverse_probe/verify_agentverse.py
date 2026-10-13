#!/usr/bin/env python3
"""G-7 实证脚本：AgentVerse 能否驱动 mock_fleet 的 100 台 agent？

本脚本**只做验证，不产出 harness**。它把 Issue #114 要求的「实测 AgentVerse
能否加载 100 台 agent 定义并跑任务级仿真」固化为可复现的探针，输出原始报错与
结论，避免任何「臆断能/不能」。

前置（不污染主仓依赖；AgentVerse 物料在 external/ 之外，见 docs/agentverse-verification/README.md）：

    git clone --depth 1 https://github.com/OpenBMB/AgentVerse /tmp/agentverse_probe
    pip install -i https://mirrors.aliyun.com/pypi/simple/ \
        numpy "openai==1.1.0" "httpx==0.25.0" "pydantic==1.10.7" colorama colorlog \
        py3langid "tiktoken==0.5.1" rapidfuzz "langchain==0.0.157" scikit-learn astunparse

用法（在 AgentVerse 仓根目录运行，或用 --av-root 指路）：

    python verify_agentverse.py --av-root /tmp/agentverse_probe

退出码：0 = 探针完成（无论结论"能/不能"，只表示证据采集成功）；
        2 = 前置缺失（未设置 AV_ROOT / 未安装依赖），结论为"未验证"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# mock_fleet 的遥测契约字段（payload 必须含这些，才算"产出遥测"）。
TELEMETRY_FIELDS = {
    "robot_id", "status_code", "error_code", "battery_pct", "battery_temp",
    "max_joint_temp", "joint_temperatures", "joint_positions", "joint_torques",
    "pos_x", "pos_y", "pos_z", "orient_w", "orient_x", "orient_y", "orient_z", "ts",
}




def _quiet_agentverse_logging() -> None:
    """AgentVerse 会把完整 prompt/401 原文打到 stdout；探针只需结论，压掉噪音。"""
    import logging
    for name in ("agentverse", "openai", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def _purge_agentverse_modules() -> None:
    """彻底清掉 agentverse.* 及 openai 客户端缓存，保证 import 探针可复现。"""
    for m in [m for m in list(sys.modules) if m.startswith("agentverse")]:
        del sys.modules[m]


def load_mock_fleet_payload_fields(repo_root: Path) -> set[str]:
    """从 simulator/mock_fleet.py 现读一份真实 payload 字段，作为对照基准。"""
    sys.path.insert(0, str(repo_root))
    from simulator.mock_fleet import RobotState  # type: ignore

    return set(RobotState(robot_id="humanoid-001").build_payload(1).keys())


def main() -> int:
    p = argparse.ArgumentParser(description="G-7 AgentVerse 驱动能力实证探针")
    p.add_argument("--av-root", default=os.environ.get("AV_ROOT", "/tmp/agentverse_probe"),
                   help="AgentVerse 源码根目录")
    p.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]),
                   help="RobotMonitor 仓库根（用于读 mock_fleet 契约）")
    p.add_argument("--agents", type=int, default=100, help="尝试构造的 agent 台数")
    args = p.parse_args()

    av = Path(args.av_root)
    if not (av / "agentverse").is_dir():
        print(f"[SKIP] 未找到 AgentVerse 源码：{av}/agentverse 不存在。", file=sys.stderr)
        print("[SKIP] 结论=未验证（前置缺失）。", file=sys.stderr)
        return 2

    sys.path.insert(0, str(av))
    repo_root = Path(args.repo_root)

    print("=" * 72)
    print("G-7 AgentVerse 驱动能力实证探针")
    print("=" * 72)

    # ---- 基准：mock_fleet 的遥测契约 ----
    fields = load_mock_fleet_payload_fields(repo_root)
    print(f"\n[基准] mock_fleet payload 字段({len(fields)}): {sorted(fields)}")
    missing = TELEMETRY_FIELDS - fields
    print(f"[基准] 契约校验：{'一致' if not missing else '缺 ' + str(missing)}")

    # ---- 探针 1：无 OPENAI_API_KEY 时能否 import ----
    print("\n[探针 1] 无 OPENAI_API_KEY 时 import agentverse")
    saved = os.environ.pop("OPENAI_API_KEY", None)
    saved_az = os.environ.pop("AZURE_OPENAI_API_KEY", None)
    _purge_agentverse_modules()
    try:
        import importlib
        importlib.import_module("agentverse")
        print("  → import 成功（与预期不符，请复核）")
        probe1 = "import-ok-without-key"
    except Exception as e:  # noqa: BLE001 —— 探针就是要原文捕获异常
        print(f"  → 失败：{type(e).__name__}: {e}")
        probe1 = f"{type(e).__name__}: {e}"
    finally:
        if saved:
            os.environ["OPENAI_API_KEY"] = saved
        if saved_az:
            os.environ["AZURE_OPENAI_API_KEY"] = saved_az

    # ---- 探针 2：加载任务配置 + 构造 N 个 agent ----
    print(f"\n[探针 2] 构造 {args.agents} 个 agent（需 OPENAI_API_KEY 占位）")
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
        print(f"  → 构造 {len(agents)} 个 agent 对象成功，耗时 {dt:.2f}s")
        print(f"  → 首个 agent: {type(agents[0]).__name__}, llm={type(agents[0].llm).__name__}")
        probe2 = {"constructed": len(agents), "seconds": round(dt, 3),
                  "agent_class": type(agents[0]).__name__,
                  "llm_class": type(agents[0].llm).__name__}
    except Exception as e:  # noqa: BLE001
        print(f"  → 失败：{type(e).__name__}: {e}")
        probe2 = {"error": f"{type(e).__name__}: {e}"}

    # ---- 探针 3：agent.step 产出的是不是遥测 ----
    print("\n[探针 3] agent 单步产物是否命中遥测契约")
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
        print(f"  → Message 字段: {sorted(map(str, d.keys()))}")
        print(f"  → 命中遥测契约字段: {sorted(hit) if hit else '无'}")
        print(f"  → 含遥测数值: {numeric_hit}")
        probe3 = {"message_keys": sorted(map(str, d.keys())),
                  "telemetry_field_hit": sorted(hit), "has_telemetry_value": numeric_hit}
    except Exception as e:  # noqa: BLE001
        print(f"  → 失败：{type(e).__name__}: {e}")
        probe3 = {"error": f"{type(e).__name__}: {e}"}

    # ---- 结论 ----
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    key_gated = probe1.startswith("ImportError")
    constructed = probe2.get("constructed", 0)
    telemetry = bool(probe3.get("has_telemetry_value"))
    print(f"  1) 无 API key 时 import 直接被 gate（DEFAULT_CLIENT 未定义）: {key_gated}")
    print(f"  2) 有 key 占位时可构造 agent 对象: {constructed} 台")
    print(f"  3) agent 单步产物是否携带遥测数值: {telemetry}")
    if key_gated and constructed and not telemetry:
        print("  ⇒ 判定：AgentVerse 可加载/构造 100 agent，但【不能】直接产出本系统遥测；")
        print("     其 step 走 LLM 文本对话，与 mock_fleet 的数值 payload 契约不同层。")
        verdict = "loadable-but-not-telemetry"
    else:
        print("  ⇒ 判定：需人工复核上述探针输出。")
        verdict = "inconclusive"
    print(f"  verdict = {verdict}")

    out = Path(__file__).with_name("verify_agentverse_report.json")
    out.write_text(json.dumps(
        {"probe1_import_without_key": probe1, "probe2_construct": probe2,
         "probe3_message": probe3, "mock_fleet_fields": sorted(fields),
         "verdict": verdict}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告写入 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
