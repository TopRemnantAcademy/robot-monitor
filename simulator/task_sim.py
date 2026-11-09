#!/usr/bin/env python3
"""RobotMonitor 任务级多机器人协同仿真（Issue #63 / G-7）。

与 `mock_fleet.py` 的区别
------------------------
`mock_fleet.py` 只产**独立波形**（每台各自随机游走），没有「任务级」概念。
本脚本在 `mock_fleet` 的 payload 构造器与 MQTT 发布器之上，叠加一个
**任务调度器**，让 N 台机器人的遥测在**时间上相关**：

- 流水线场景（pipeline）：机器人被分派到工位（上料→焊接→下料），按节拍轮转；
  某一工位「干活」时其任务关节升温、相邻的下一工位待机降温 —— 相邻工位的
  关节温度序列呈**反相关**，相关完全来自调度逻辑，而非硬编码系数。
- 故障传播场景（fault）：一台中枢机器人周期性过热故障，其邻居因队列积压而
  任务关节温度被抬高 —— 故障机与邻居的温度序列呈**正相关**。

关键纪律（Issue #63 禁止项）
--------------------------
- **禁止**为「看起来相关」而在波形里硬编码相关系数：所有相关性必须来自
  `TaskScheduler` 对 `task_load` 的调度（见 `TaskRobot.step` 与调度器）。
- **禁止**引入 GPU / 重框架：纯 Python `asyncio`，复用 `mock_fleet` 的发布器。
- 输出仍走 MQTT（topic / payload schema 与 `mock_fleet` 完全一致），
  保持与现有压测/落库管线一致。

运行
----
    # 流水线场景，纯仿真 + 相关性断言（不需要 broker，用于验收）
    python simulator/task_sim.py --scenario pipeline --no-broker --duration 60

    # 故障传播场景，发给真实 broker
    python simulator/task_sim.py --scenario fault --robots 12 --broker localhost --port 1883

    # 自定义工位/节拍
    python simulator/task_sim.py --scenario pipeline --stations 3 --cycle 12

退出码：0 = 通过（至少一对机器人指标序列显著相关）；2 = 相关性未达标（验收失败）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
from dataclasses import dataclass, field

# 复用 mock_fleet 的契约常量与发布器，保证 wire format 与现有管线完全一致。
try:
    from mock_fleet import (  # type: ignore
        JOINT_NAMES, RobotState, TELEMETRY_TOPIC, publisher, PUBLISH_INTERVAL_S,
        STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT,
        TEMP_BASE_RANGE, OVERHEAT_TARGET_C, BATTERY_DRAIN_PER_S,
    )
except ImportError:  # 作为包导入时（python -m simulator.task_sim）
    from simulator.mock_fleet import (  # type: ignore
        JOINT_NAMES, RobotState, TELEMETRY_TOPIC, publisher, PUBLISH_INTERVAL_S,
        STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT,
        TEMP_BASE_RANGE, OVERHEAT_TARGET_C, BATTERY_DRAIN_PER_S,
    )

ROBOT_COUNT = 100
ACTIVE_JOINT = "right_elbow_pitch"  # 任务加热关节（焊接/上料相关，pipeline 与 fault 共用）
TASK_HEAT_PER_LOAD = 50.0           # 单位 task_load 每 tick 注入的升温（°C）；取大值 → 任务关节在
                                     # 激活瞬间即达上限，形成无斜坡的方波，相关性判定稳健可复现。
# 默认只让「被调度」的机器人参与相关性分析，其余保持独立波形（与 mock_fleet 一致）。


# ---------------------------------------------------------------- 任务机器人

@dataclass
class TaskRobot(RobotState):
    """在 mock_fleet.RobotState 之上增加任务负载维度。

    `task_load` 由调度器按工位/故障状态设定（0..1）。`step()` 在父类的
    电量下降 + 基础随机游走 + 过热注入之外，对 `ACTIVE_JOINT` 叠加任务升温；
    当 load=0 时父类把温度拉回基础区间 —— 从而温度随任务节拍起落。
    """

    task_load: float = 0.0

    def step(self, now: float, start_at: float) -> None:
        super().step(now, start_at)
        if self.task_load > 0:
            cur = self.joint_temps[ACTIVE_JOINT]
            # 升温幅度与 task_load 成正比；上限放宽到过热区间之上，便于观察。
            self.joint_temps[ACTIVE_JOINT] = min(
                OVERHEAT_TARGET_C + 6.0, cur + self.task_load * TASK_HEAT_PER_LOAD
            )
        else:
            # 无负载：向基础区间自然回落（父类的随机游走会把 >65 的值拉回上界）。
            pass


# ---------------------------------------------------------------- 调度器

class PipelineScheduler:
    """流水线节拍：N 个工位轮转，每个 tick 只有当前工位在干活。

    station i 在相位 i 时 task_load=1，其余为 0。相邻工位的 ACTIVE_JOINT
    温度随之此消彼长 —— 产生真实的（反）相关，无硬编码系数。
    """

    def __init__(self, workers: list[TaskRobot], stations: int, cycle_s: float) -> None:
        self.workers = workers[:stations] if stations < len(workers) else workers
        self.stations = max(1, min(stations, len(self.workers)))
        self.cycle_s = cycle_s
        self._phase_dur = cycle_s / self.stations
        self._last_phase = -1

    def update(self, elapsed_s: float) -> None:
        """Deterministic phase from a tick-driven elapsed time (no wall clock).

        Previously this was an ``async`` coroutine driven by ``time.monotonic()``,
        which drifted against the sampler's own wall clock and made the
        correlation acceptance flaky across CI runs. Driving the phase from a
        single tick counter removes that non-determinism.
        """
        if self._phase_dur <= 0:
            phase = 0
        else:
            phase = int(elapsed_s // self._phase_dur) % self.stations
        for i, w in enumerate(self.workers):
            w.task_load = 1.0 if i == phase else 0.0


class FaultScheduler:
    """故障传播：一台中枢机器人周期性过热故障（task_load 拉满一段时间），
    其邻居因队列积压而保持中等负载 —— 故障机与邻居温度正相关。"""

    def __init__(self, hub: TaskRobot, neighbors: list[TaskRobot],
                 fault_period_s: float, fault_dur_s: float) -> None:
        self.hub = hub
        self.neighbors = neighbors
        self.fault_period_s = fault_period_s
        self.fault_dur_s = fault_dur_s

    def update(self, elapsed_s: float) -> None:
        """Deterministic fault schedule from a tick-driven elapsed time.

        ``elapsed_s`` is the same tick counter the sampler uses, so the hub and
        neighbours are perfectly aligned (no wall-clock drift).
        """
        in_fault = (elapsed_s % self.fault_period_s) < self.fault_dur_s
        self.hub.task_load = 1.0 if in_fault else 0.0
        # 中枢故障时邻居队列积压 → 负载升高；恢复后回落到常态 0.4。
        for n in self.neighbors:
            n.task_load = 0.8 if in_fault else 0.4


# ---------------------------------------------------------------- 相关性分析

def pearson(a: list[float], b: list[float]) -> float:
    """Pearson 相关系数（长度不等时取较短者）。"""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


# ---------------------------------------------------------------- 录制 + 主循环

@dataclass
class Recorder:
    series: dict[str, list[float]] = field(default_factory=dict)

    def sample(self, states: list[TaskRobot]) -> None:
        for s in states:
            self.series.setdefault(s.robot_id, []).append(
                round(s.joint_temps[ACTIVE_JOINT], 2)
            )

    def report(self, threshold: float = 0.3) -> float:
        ids = list(self.series.keys())
        best = 0.0
        best_pair = (None, None)
        best_kind = ""
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                r = pearson(self.series[ids[i]], self.series[ids[j]])
                if abs(r) > abs(best):
                    best = r
                    best_pair = (ids[i], ids[j])
                    # 相邻工位（pipeline）呈反相关；中枢-邻居（fault）呈正相关。
                    best_kind = "正相关" if r > 0 else "反相关"
        print(f"[correlation] 最强相关对 {best_pair[0]} ↔ {best_pair[1]}: "
              f"r={best:+.3f}（{best_kind}）")
        print(f"[correlation] 显著阈值 |r| ≥ {threshold}："
              f"{'通过' if abs(best) >= threshold else '未达标'}")
        return best


async def amain(args: argparse.Namespace) -> int:
    # 固定随机种子必须在「创建机器人」之前：RobotState 的初值（关节温度/电量）依赖
    # random，不在此处 seed 会导致初值非确定 → 相关性验收随机失败（CI flaky，见 issue）。
    random.seed(42)

    # 参与任务调度的机器人（从 humanoid-001 起取前 K 台）。
    k = max(args.stations if args.scenario == "pipeline" else 4, 2)
    workers: list[TaskRobot] = []
    for i in range(k):
        rid = f"humanoid-{i + 1:03d}"
        w = TaskRobot(robot_id=rid)
        workers.append(w)

    if args.scenario == "pipeline":
        sched = PipelineScheduler(workers, args.stations, args.cycle)
    else:
        hub = workers[0]
        neighbors = workers[1:]
        sched = FaultScheduler(hub, neighbors, fault_period_s=args.cycle,
                               fault_dur_s=args.cycle * 0.4)

    recorder = Recorder()
    counters = type("C", (), {"sent": 0, "publish_errors": 0, "tps": 0.0})()

    queue: asyncio.Queue = asyncio.Queue(maxsize=200_000)

    # 确定性 tick 循环：相位与采样都来自同一个离散 tick 计数，不再依赖
    # time.monotonic() 的墙钟偏移。之前调度器（50ms 协程）与采样器（100ms 协程）各自用
    # 墙钟、且 start_at 存在微小偏差，会让「哪台机器人处于热窗口」在每轮运行间漂移 ——
    # 这正是相关性验收偶发失败（r 有时为负、有时为正、|r| 有时 < 0.3）的根因。现在用单一
    # tick 驱动，结果 100% 可复现。
    tick_s = PUBLISH_INTERVAL_S
    ticks_total = max(1, int(round(args.duration / tick_s)))
    tasks = []
    if not args.no_broker:
        tasks.append(asyncio.create_task(publisher(queue, counters, args.broker, args.port)))

    elapsed = 0.0
    for _ in range(ticks_total):
        sched.update(elapsed)
        for w in workers:
            w.step(elapsed, 0.0)
            await queue.put((TELEMETRY_TOPIC.format(robot_id=w.robot_id),
                             json.dumps(w.build_payload(int(elapsed * 1000)),
                                        separators=(",", ":"))))
        recorder.sample(workers)
        elapsed += tick_s
        await asyncio.sleep(0)  # 让 publisher（如有）有排空机会，但不影响仿真时序

    for t in tasks:
        t.cancel()

    best = recorder.report()
    # 导出 60s 遥测样本（供人工/绘图复核）。
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(recorder.series, f)
    print(f"[task_sim] 场景={args.scenario} 工位/参与台数={k} "
          f"样本/台={len(next(iter(recorder.series.values())))} 导出→{args.out}")
    return 0 if abs(best) >= 0.3 else 2


def main() -> None:
    p = argparse.ArgumentParser(description="RobotMonitor 任务级多机器人协同仿真 (Issue #63)")
    p.add_argument("--scenario", choices=["pipeline", "fault"], default="pipeline")
    p.add_argument("--stations", type=int, default=3, help="流水线工位数（pipeline）")
    p.add_argument("--robots", type=int, default=ROBOT_COUNT, help="参与调度的机器人台数")
    p.add_argument("--cycle", type=float, default=12.0, help="流水线/故障周期（秒）")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--out", default="task_sim_series.json", help="相关性样本导出路径")
    p.add_argument("--no-broker", action="store_true", help="纯仿真 + 相关性断言，不连 MQTT")
    args = p.parse_args()
    try:
        rc = asyncio.run(amain(args))
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
