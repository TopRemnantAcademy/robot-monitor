#!/usr/bin/env python3
"""RobotMonitor task-level multi-robot coordinated simulation (Issue #63 / G-7).

How this differs from `mock_fleet.py`
-------------------------------------
`mock_fleet.py` only produces **independent waveforms** (each robot random-walks on its own)
and has no "task-level" concept. This script builds on `mock_fleet`'s payload builder and MQTT
publisher, adding a **task scheduler** so that the telemetry of N robots becomes **time-correlated**:

- Pipeline scenario: robots are assigned to stations (load -> weld -> unload) and rotate by beat;
  when a station is "working", its task joint heats up while the next station idles and cools down
  -> the neighbouring stations' joint-temperature series are **negatively correlated**, and the
  correlation comes entirely from the scheduler, not from hard-coded coefficients.
- Fault-propagation scenario: a hub robot overheats periodically; its neighbours' task-joint
  temperature is raised by queue backlog -> the hub and its neighbours are **positively correlated**.

Key discipline (Issue #63 forbidden items)
-----------------------------------------
- **Forbidden**: hard-coding a correlation coefficient into the waveform just to "look correlated".
  All correlation must come from `TaskScheduler`'s scheduling of `task_load` (see `TaskRobot.step`
  and the schedulers).
- **Forbidden**: introducing GPU / heavy frameworks. Pure Python `asyncio`, reusing mock_fleet's publisher.
- Output still goes over MQTT (topic / payload schema identical to `mock_fleet`), keeping the
  existing load-test / landing pipeline consistent.

Run
---
    # Pipeline scenario, pure simulation + correlation assertion (no broker needed, for acceptance)
    python simulator/task_sim.py --scenario pipeline --no-broker --duration 60

    # Fault-propagation scenario, send to a real broker
    python simulator/task_sim.py --scenario fault --robots 12 --broker localhost --port 1883

    # Custom stations / cycle
    python simulator/task_sim.py --scenario pipeline --stations 3 --cycle 12

Exit code: 0 = passed (at least one pair of robot metric series is significantly correlated);
2 = correlation below threshold (acceptance failed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
from dataclasses import dataclass, field

# Reuse mock_fleet's contract constants and publisher so the wire format is identical to the existing pipeline.
try:
    from mock_fleet import (  # type: ignore
        JOINT_NAMES, RobotState, TELEMETRY_TOPIC, publisher, PUBLISH_INTERVAL_S,
        STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT,
        TEMP_BASE_RANGE, OVERHEAT_TARGET_C, BATTERY_DRAIN_PER_S,
    )
except ImportError:  # imported as a package (python -m simulator.task_sim)
    from simulator.mock_fleet import (  # type: ignore
        JOINT_NAMES, RobotState, TELEMETRY_TOPIC, publisher, PUBLISH_INTERVAL_S,
        STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT,
        TEMP_BASE_RANGE, OVERHEAT_TARGET_C, BATTERY_DRAIN_PER_S,
    )

ROBOT_COUNT = 100
ACTIVE_JOINT = "right_elbow_pitch"  # the task-heated joint (weld/load related, shared by pipeline and fault)
TASK_HEAT_PER_LOAD = 50.0           # temperature rise per tick per unit task_load (°C); a large value -> the
                                     # task joint hits the ceiling instantly, forming a ramp-less square wave so the
                                     # correlation judgement is robust and reproducible.
# By default, only "scheduled" robots participate in correlation analysis; the rest keep independent
# waveforms (consistent with mock_fleet).


# ---------------------------------------------------------------- task robot

@dataclass
class TaskRobot(RobotState):
    """Adds a task-load dimension on top of mock_fleet.RobotState.

    `task_load` is set by the scheduler per station / fault state (0..1). `step()` adds task heating
    to `ACTIVE_JOINT` on top of the parent's battery drain + base random-walk + overheat injection;
    when load=0 the parent pulls the temperature back into the base range -> temperature rises and
    falls with the task beat.
    """

    task_load: float = 0.0

    def step(self, now: float, start_at: float) -> None:
        super().step(now, start_at)
        if self.task_load > 0:
            cur = self.joint_temps[ACTIVE_JOINT]
            # Heating amplitude is proportional to task_load; the ceiling is relaxed above the overheat
            # range for visibility.
            self.joint_temps[ACTIVE_JOINT] = min(
                OVERHEAT_TARGET_C + 6.0, cur + self.task_load * TASK_HEAT_PER_LOAD
            )
        else:
            # No load: naturally fall back toward the base range (the parent's random walk pulls >65 back to the ceiling).
            pass


# ---------------------------------------------------------------- schedulers

class PipelineScheduler:
    """Pipeline beat: N stations rotate; each tick only the current station is working.

    Station i has task_load=1 at phase i, others 0. The neighbouring stations' ACTIVE_JOINT
    temperatures ebb and flow accordingly -> genuine (negative) correlation, no hard-coded coefficients.
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
    """Fault propagation: a hub robot overheats periodically (task_load maxed for a while),
    and its neighbours stay at a medium load due to queue backlog -> the hub and neighbours are
    positively correlated."""

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
        # When the hub faults, neighbours' queue backlog -> load rises; after recovery it falls back to 0.4.
        for n in self.neighbors:
            n.task_load = 0.8 if in_fault else 0.4


# ---------------------------------------------------------------- correlation analysis

def pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation coefficient (takes the shorter when lengths differ)."""
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


# ---------------------------------------------------------------- recorder + main loop

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
                    # Neighbouring stations (pipeline) are negatively correlated; hub-neighbour (fault) positive.
                    best_kind = "positive" if r > 0 else "negative"
        print(f"[correlation] strongest pair {best_pair[0]} <-> {best_pair[1]}: "
              f"r={best:+.3f} ({best_kind})")
        print(f"[correlation] significance threshold |r| >= {threshold}: "
              f"{'passed' if abs(best) >= threshold else 'below threshold'}")
        return best


async def amain(args: argparse.Namespace) -> int:
    # The fixed random seed MUST be set before "creating robots": RobotState's initial values
    # (joint temperatures / battery) depend on random; not seeding here makes the initial values
    # non-deterministic -> correlation acceptance randomly fails (CI flaky, see issue).
    random.seed(42)

    # Robots participating in task scheduling (take the first K, starting from humanoid-001).
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

    # Deterministic tick loop: phase and sampling both come from the same discrete tick counter,
    # no longer depending on time.monotonic() wall-clock skew. Previously the scheduler (50ms
    # coroutine) and the sampler (100ms coroutine) each used their own wall clock and had a tiny
    # start_at offset, so "which robot is in a hot window" drifted between runs -- that was the root
    # cause of the occasional correlation-acceptance failure (r sometimes negative, sometimes
    # positive, |r| sometimes < 0.3). Now a single tick drives it, 100% reproducible.
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
        await asyncio.sleep(0)  # let the publisher (if any) drain, without affecting simulation timing

    for t in tasks:
        t.cancel()

    best = recorder.report()
    # Export 60s telemetry samples (for manual / plotting review).
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(recorder.series, f)
    print(f"[task_sim] scenario={args.scenario} stations/participating={k} "
          f"samples/robot={len(next(iter(recorder.series.values())))} exported->{args.out}")
    return 0 if abs(best) >= 0.3 else 2


def main() -> None:
    p = argparse.ArgumentParser(description="RobotMonitor task-level multi-robot coordinated simulation (Issue #63)")
    p.add_argument("--scenario", choices=["pipeline", "fault"], default="pipeline")
    p.add_argument("--stations", type=int, default=3, help="number of pipeline stations (pipeline)")
    p.add_argument("--robots", type=int, default=ROBOT_COUNT, help="number of robots in scheduling")
    p.add_argument("--cycle", type=float, default=12.0, help="pipeline / fault cycle (seconds)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--out", default="task_sim_series.json", help="correlation-sample export path")
    p.add_argument("--no-broker", action="store_true", help="pure simulation + correlation assertion, no MQTT")
    args = p.parse_args()
    try:
        rc = asyncio.run(amain(args))
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
