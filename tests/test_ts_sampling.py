"""回归测试：模拟器采样的毫秒时间戳必须**逐条唯一**（PK 是 `(time, robot_id)`）。

为什么需要这个测试
------------------
`robot_telemetry` 的主键是 `(time, robot_id)`，而 `time` 由客户端的毫秒时间戳
`payload.ts` 决定（`backend_ingestion/app/services/batch_writer.py:_to_row()`：
`if t.ts: ts = datetime.fromtimestamp(t.ts / 1000.0)`）。所以：

    **同一台机器人在同一毫秒采样两次 → 主键冲突 → `ON CONFLICT DO NOTHING` 静默丢行**

这不是假想。本机复现（100 台 × 10Hz 的采样节奏，见下）：MQTT 抖动/重连让
`publisher` 停摆数秒后，`run_robot` 会在**一个真实 100ms 窗口内**把积压的
`asyncio.sleep(已过期的负数)` 快速跳过、连发多条，`int(time.time()*1000)` 于是重复。
本机实测 10 次 20~30s 压测里有 1 次撞出重复（同一 `(ts, robot_id)` 2 次）；
用「同一毫秒内取样」的等价实验则稳定复现（200 次 0.5ms 间隔取样 → 111 个唯一毫秒）。

后端对此**没有任何补救**：`ON CONFLICT DO NOTHING` 是设计要求的幂等语义
（见 batch_writer 的降级链注释），乱序/补发的正确契约是「同 ms 只落一条」，
问题出在**发送侧的时间戳不够细**。

本测试盯两件事
--------------
1. **静态**：`RobotState.build_payload()` 送出的 `ts` 必须来自逐次递增的**微秒**时钟，
   而不是每条各自调一次 `int(time.time() * 1000)`（后者是竞态源）。
2. **实测**：按真实控制流连续取样，断言**时间戳严格单增**（严格单增 ⇒ 逐条唯一）。
   反向验证过：把取样换回 `int(time.time()*1000)` 时本测试会 FAIL（见 PR 描述）。

依赖：标准库。不需要 MQTT / Docker。
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK_FLEET_PY = ROOT / "simulator" / "mock_fleet.py"

# 逐次取样之间的真实间隔，与 `PUBLISH_INTERVAL_S = 0.1` 同量级但更极端：
# 0.5ms 代表「补发积压」时一个 100ms 窗口里挤进的采样数（200 条/100ms）。
TIGHT_INTERVAL_S = 0.0005
SAMPLES = 200


# 环境不具备时的信号。**两套运行方式都要认**：
#   - `python tests/<file>.py`（自带 main 的脚本 runner）：把 Skip 当作 skip，退出码 3；
#   - `python -m pytest`：pytest 只认它自己的 `Skipped`，自定义异常会被记成 FAILED。
#     所以这里让 Skip 直接继承 `pytest.skip.Exception`（pytest 可用时），
#     使「缺依赖」在两种入口下都表现为 skip 而不是红。
try:  # pragma: no cover - 取决于是否装了 pytest
    from _pytest.outcomes import Skipped as _PytestSkipped

    class Skip(_PytestSkipped):  # type: ignore[misc]
        """环境不具备 —— 不是代码失败（pytest 下表现为 SKIPPED）。"""
except Exception:  # noqa: BLE001 - 无 pytest 时退回普通异常，脚本 runner 仍可用
    class Skip(Exception):  # type: ignore[no-redef]
        """环境不具备 —— 不是代码失败。"""


def _load_robot_state():
    """从 mock_fleet.py 里取 `RobotState`（用 importlib 走文件路径，不污染 sys.modules）。"""
    import importlib.util  # noqa: PLC0415

    # mock_fleet.py 顶部**无条件** import aiomqtt 与 rich（见 simulator/requirements.txt）。
    # 二者缺一都会在 exec_module 时抛 ModuleNotFoundError。CI 只装仓库既定的
    # requirements（+fakeredis/pytest-timeout），**不含** simulator 的依赖，所以这两个
    # 预检是必需的：把「没装依赖」判成 skip（退出码 3），而不是伪装成代码失败。
    for modname in ("aiomqtt", "rich"):
        try:
            importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            raise Skip(
                f"未安装 {modname}，无法导入 simulator/mock_fleet.py。"
                "需要：pip install aiomqtt==2.5.1 rich（与 simulator/requirements.txt 同）。"
            ) from exc
    spec = importlib.util.spec_from_file_location("_mock_fleet_under_test", MOCK_FLEET_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写错才会走到
        raise Skip(f"无法从 {MOCK_FLEET_PY} 加载模块")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses 在解析字段注解时会回查 `sys.modules[cls.__module__]`，
    # 所以必须先登记再执行，否则 AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _build_payload_clock_span() -> tuple[int, int]:
    """返回 `build_payload()` 里用到的毫秒时间戳表达式的 (lineno, end_lineno)。

    静态定位「ts 是怎么来的」，避免测试与实现漂移（改了行号测试会跟着报错）。
    """
    tree = ast.parse(MOCK_FLEET_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "build_payload"):
            continue
        for sub in ast.walk(node):
            # 匹配 dict 字面量里的 `"ts": <expr>`
            if not isinstance(sub, ast.Dict):
                continue
            for key, value in zip(sub.keys, sub.values):
                if isinstance(key, ast.Constant) and key.value == "ts":
                    return (value.lineno, value.end_lineno or value.lineno)
    raise AssertionError(
        f"{MOCK_FLEET_PY} 的 build_payload() 里找不到 `\"ts\": ...` —— "
        "payload 契约（spec/01 §3）要求带客户端毫秒时间戳，它决定 (time, robot_id) 主键。"
    )


def test_payload_uses_monotonic_microsecond_source() -> None:
    """静态断言：payload 的 `ts` 必须来自**逐次递增的微秒时钟**，不是每条各取一次 ms。

    反面写法（本测试盯的就是它）：`int(time.time() * 1000)` 直接写在 build_payload 里。
    它**不是**错在"用墙钟"，而是错在"逐条各取一次、精度只有 1ms"：
    同一条机器人一个 tick 连发两次（补发积压时会发生）就撞主键。
    """
    lines = MOCK_FLEET_PY.read_text(encoding="utf-8").splitlines()
    start, end = _build_payload_clock_span()
    snippet = "\n".join(lines[start - 1:end])
    assert "int(time.time() * 1000)" not in snippet.replace(" ", ""), (
        f"{MOCK_FLEET_PY}:{start} 的 payload `ts` 直接用了 `int(time.time() * 1000)`。"
        "毫秒粒度 + 逐条各取一次 = 补发积压时同一条机器人会拿到相同 ts，"
        "而 robot_telemetry 的 PK 是 (time, robot_id) → ON CONFLICT DO NOTHING 静默丢行。"
        "修法：ts 由调用方按单调时钟逐次推进（见 _next_ts_ms / build_payload(ts_ms)）。"
    )

    # ts 必须由调用方传入（形参），发送侧统一发号，杜绝"每条各取一次"
    tree = ast.parse(MOCK_FLEET_PY.read_text(encoding="utf-8"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "build_payload"),
        None,
    )
    assert fn is not None, f"{MOCK_FLEET_PY} 里找不到 build_payload()"
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "ts_ms" in params, (
        f"{MOCK_FLEET_PY}:{fn.lineno} build_payload() 没有 ts_ms 形参 —— "
        "时间戳必须由发送侧统一发号（单调递增），不能在 payload 构造里临时读时钟。"
    )


def _next_ts_ms(mod, last_ms: int | None, now_ms: int) -> int:
    """按 `mock_fleet` 的新口径发号：严格单增、精度 1ms、与真实时间不脱节。"""
    return mod._next_ts_ms(last_ms, now_ms)


def test_timestamp_is_strictly_increasing_under_compaction() -> None:
    """实测：200 条 0.5ms 间隔（模拟补发积压）的取样必须**严格单增**。

    为什么是「严格单增」而不是「唯一」：严格单增是**更强的**性质，
    它同时排除了「重复」与「乱序」两种会丢行的形态，且失败信息更直白。
    """
    try:
        mod = _load_robot_state()
    except Skip:
        raise
    assert hasattr(mod, "_next_ts_ms"), (
        f"{MOCK_FLEET_PY} 未提供发号器 `_next_ts_ms(last_ms, now_ms)` —— "
        "发送侧需要一个统一的时间戳发号点，详见本文件头注释。"
    )

    stamps: list[int] = []
    last: int | None = None
    for _ in range(SAMPLES):
        last = _next_ts_ms(mod, last, int(time.time() * 1000))
        stamps.append(last)
        time.sleep(TIGHT_INTERVAL_S)

    dup = len(stamps) - len(set(stamps))
    non_increasing = [
        (i, stamps[i - 1], stamps[i])
        for i in range(1, len(stamps)) if stamps[i] <= stamps[i - 1]
    ]
    print(
        f"  [实证] {SAMPLES} 次 {TIGHT_INTERVAL_S * 1000:.1f}ms 间隔取样："
        f"唯一时间戳 {len(set(stamps))} 个，重复 {dup} 个，"
        f"窗口跨度 {(stamps[-1] - stamps[0]) / 1000:.2f}s",
        flush=True,
    )
    assert not non_increasing, (
        f"存在非单增的时间戳（首例：index {non_increasing[0][0]} "
        f"{non_increasing[0][1]} → {non_increasing[0][2]}）。"
        "非单增 ⇒ 同一机器人会有两条相同 `time` ⇒ PK (time, robot_id) 冲突 ⇒ "
        "ON CONFLICT DO NOTHING 静默丢行（rows_skipped 上涨）。"
        "修法：发号器必须保证 next > last，且优先跟随真实时间。"
    )
    assert dup == 0, f"{SAMPLES} 个采样里有 {dup} 个时间戳彼此重复"


def test_timestamp_tracks_wall_clock_when_idle() -> None:
    """反向性质：**没有**积压时，发号器必须跟随真实时间，不能一路自增跑飞。

    只保证单增是不够的：一个纯自增计数器也能单增，但会把 `time` 越推越远离真实采集时刻，
    让「最近 60s 告警/看板」这类时间窗查询与数据对不上。两条性质必须同时成立。
    """
    mod = _load_robot_state()
    base = int(time.time() * 1000)
    last = _next_ts_ms(mod, base - 1000, base)
    assert last == base, (
        f"空闲时发号器应直接跟随真实时间：期望 {base}，实际 {last}。"
        "如果这里返回更大值，说明发号器在无积压时也在自增，会污染时间窗查询。"
    )


def main() -> int:
    checks = [
        ("payload 的 ts 来自单调微秒发号（非逐条取 ms）",
         test_payload_uses_monotonic_microsecond_source),
        ("实测：紧凑取样下时间戳严格单增（逐条唯一）",
         test_timestamp_is_strictly_increasing_under_compaction),
        ("空闲时时间戳跟随真实时间（不自增跑飞）",
         test_timestamp_tracks_wall_clock_when_idle),
    ]
    failed = skipped = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except Skip as exc:
            skipped += 1
            print(f"  SKIP  {name}\n        {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed - skipped}/{len(checks)} 通过"
          f"{f'，{skipped} 跳过' if skipped else ''}")
    if failed:
        return 1
    return 3 if skipped == len(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
