"""压测 payload 契约测试：`tests/load/payload.js` ←→ `simulator/mock_fleet.py` 逐字段对拍。

为什么需要它
------------
订单 23 的硬要求是「payload 复用 mock_fleet 真实 24 关节结构」。这句话如果
只靠人眼比对，就是一张空头支票 —— 恰恰是本仓踩过多次的坑（24 关节漏一个、
max_joint_temp 推导错，都会让压测变成「测 mqtt_dropped 计数器」）。

本文件把这条要求变成可执行断言：
  1. 从 `simulator/mock_fleet.py` **解析**出真实的关节名与 payload 字段名；
  2. 从 `tests/load/payload.js` **解析**出压测侧声明的同名常量；
  3. 断言两边相等，并断言 payload.js 产出的样例报文能通过后端 Pydantic 校验。

不引入 pytest 依赖也能跑：`python tests/load/test_payload_contract.py`。

⚠️ 本文件**不**断言数值分布（那是随机量），只断言「结构契约」——
   结构漂了必须红；数值漂了不影响判定。
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


# ------------------------------------------------------------------ 解析 mock_fleet.py

def _mock_fleet_source() -> str:
    return MOCK_FLEET.read_text(encoding="utf-8")


def mock_fleet_joint_names() -> list[str]:
    """从 mock_fleet.py 的 `JOINT_NAMES: tuple[str, ...] = (...)` 抽出 24 个键名。

    用 ast 而非正则：正则对带类型标注的赋值、跨行元组都很脆。
    """
    tree = ast.parse(_mock_fleet_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == "JOINT_NAMES":
            values = ast.literal_eval(node.value)
            return list(values)
    raise AssertionError("mock_fleet.py 未找到 JOINT_NAMES")


def mock_fleet_payload_fields() -> list[str]:
    """从 `RobotState.build_payload` 的 return 字典抽出顶层字段名（保持源码顺序）。

    build_payload 的 return 是一个 dict 字面量，ast 能精确给出键。
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
            raise AssertionError("build_payload 的 return 不是 dict 字面量")
    raise AssertionError("mock_fleet.py 未找到 build_payload")


# ------------------------------------------------------------------ 解析 payload.js

def payload_js_source() -> str:
    return PAYLOAD_JS.read_text(encoding="utf-8")


def payload_js_joint_names() -> list[str]:
    """从 payload.js 的 `export const JOINT_NAMES = [...]` 抽出字符串数组。"""
    src = payload_js_source()
    m = re.search(r"export const JOINT_NAMES = \[(.*?)\];", src, re.S)
    if not m:
        raise AssertionError("payload.js 未找到 JOINT_NAMES")
    return re.findall(r"'([^']+)'", m.group(1))


def node_build_sample() -> dict:
    """用 node 实际执行 payload.js，取一份样例报文。

    这一步是「真实运行」而非「读源码猜」：若 payload.js 语法/逻辑坏了，
    这里会直接失败，而不是靠断言侥幸通过。
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
        raise AssertionError("node 执行 payload.js 失败：\n" + proc.stderr.strip())
    return json.loads(proc.stdout)


# ------------------------------------------------------------------ 后端 schema 字段

def telemetry_schema_fields() -> list[str]:
    """从 Telemetry(BaseModel) 的类体注解抽取字段名。"""
    tree = ast.parse(TELEMETRY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Telemetry":
            fields = []
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
            if fields:
                return fields
    raise AssertionError("telemetry.py 未找到 Telemetry 字段")


# ------------------------------------------------------------------ 断言

def test_mock_fleet_exists() -> None:
    assert MOCK_FLEET.is_file(), f"缺少 {MOCK_FLEET}（模拟器是 payload 的事实来源）"
    assert PAYLOAD_JS.is_file(), f"缺少 {PAYLOAD_JS}"


def test_joint_names_match_mock_fleet() -> None:
    """① 24 关节键名与顺序，必须与 mock_fleet.py 逐字一致。"""
    expected = mock_fleet_joint_names()
    actual = payload_js_joint_names()
    assert len(expected) == 24, f"mock_fleet JOINT_NAMES 应为 24 个，实际 {len(expected)}"
    assert actual == expected, (
        "关节键名与 mock_fleet.py 不一致\n"
        f"  mock_fleet: {expected}\n  payload.js: {actual}\n"
        f"  仅 mock_fleet 有: {sorted(set(expected) - set(actual))}\n"
        f"  仅 payload.js 有: {sorted(set(actual) - set(expected))}"
    )


def test_payload_fields_match_mock_fleet() -> None:
    """② 顶层字段集合，必须与 mock_fleet.build_payload 完全相同。"""
    expected = set(mock_fleet_payload_fields())
    sample = node_build_sample()
    actual = set(sample.keys())
    assert actual == expected, (
        "payload 顶层字段与 mock_fleet.py 不一致\n"
        f"  仅 mock_fleet 有: {sorted(expected - actual)}\n"
        f"  仅 payload.js 有: {sorted(actual - expected)}"
    )


def test_sample_payload_passes_backend_schema_shape() -> None:
    """③ 样例报文必须满足后端 Telemetry 的形状约束（不引入 pydantic 也能判）。"""
    sample = node_build_sample()
    fields = set(telemetry_schema_fields())

    # 后端声明的字段里，除这些可选姿态/时间字段外，压测都必须发出
    optional = {"battery_temp", "max_joint_temp", "pos_x", "pos_y", "pos_z",
                "orient_w", "orient_x", "orient_y", "orient_z", "ts"}
    required = fields - optional
    missing = required - set(sample.keys())
    assert not missing, f"样例报文缺后端必填字段：{sorted(missing)}"

    # robot_id 必须匹配 ^humanoid-[0-9]{3}$（telemetry.py:27）
    assert re.fullmatch(r"humanoid-[0-9]{3}", sample["robot_id"]), (
        f"robot_id 不匹配 ^humanoid-[0-9]{{3}}$: {sample['robot_id']!r}"
    )

    # 三个关节字典键必须相同（telemetry.py:48-54）且都是 24 键
    names = set(payload_js_joint_names())
    for key in ("joint_temperatures", "joint_positions", "joint_torques"):
        got = set(sample[key].keys())
        assert got == names, f"{key} 键集合不等于 spec/00 §5.5 的 24 键：{sorted(got ^ names)}"

    # max_joint_temp 必须等于最高关节温度（telemetry.py:57-62 的预计算契约）
    assert sample["max_joint_temp"] == max(sample["joint_temperatures"].values()), (
        "max_joint_temp != max(joint_temperatures)"
    )

    # battery_pct 在 [0, 100]
    assert 0.0 <= sample["battery_pct"] <= 100.0


def test_payload_size_in_expected_range() -> None:
    """④ 单条报文大小应与 mock_fleet 同量级（~2 KB），防止误发精简版。"""
    size = len(json.dumps(node_build_sample(), separators=(",", ":")))
    assert 1500 <= size <= 3500, f"单条 payload {size} B，超出 mock_fleet 同构区间 [1500, 3500]"


def main() -> int:
    checks = [
        ("payload.js / mock_fleet.py 存在", test_mock_fleet_exists),
        ("24 关节键名与顺序逐字一致", test_joint_names_match_mock_fleet),
        ("顶层字段与 mock_fleet 一致", test_payload_fields_match_mock_fleet),
        ("样例报文满足后端 schema 形状", test_sample_payload_passes_backend_schema_shape),
        ("报文大小与 mock_fleet 同量级", test_payload_size_in_expected_range),
    ]
    failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
