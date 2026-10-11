#!/usr/bin/env python3
"""RobotMonitor 遥测模拟器：100 台人形机器人 @10Hz MQTT 遥测 + 异常注入。

依据规范
--------
- spec/04_mock_robot_telemetry_simulator.md
- spec/00_system_architecture_spec.md §5.5（24 个关节标准键名，唯一事实来源）
- spec/01_backend_data_ingestion_and_storage.md §3（payload schema）

设计要点（性能与正确性）
----------------------
1. 100 个机器人协程**只负责生成 payload**，统一投递到 `asyncio.Queue`；
   由**单个 publisher 协程**串行执行 MQTT publish。
   原因：`aiomqtt.Client` 不适合多协程并发写，100 协程直接 publish 会产生
   "pending publish calls" 堆积（实测确认）。串行化后吞吐稳定在 ~1000 msg/s。
2. 三类异常注入：过热（82°C+）/ 电量耗尽（<15%）/ 随机掉线。
3. `rich.live.Live` **每秒刷新一次**汇总面板，绝不逐条 print（否则冲垮终端）。

`--duration N`：跑到第 N 秒即自行退出（用于 e2e/CI 受控压测，见 tests/e2e/）。
`--no-ui`：关掉 rich 面板，改用每秒一行的普通日志；容器里没有 TTY 时 Live 会退化成
逐帧刷屏日志，把面板关掉才能让 `docker compose logs` 可读（e2e harness 用这条）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import time
from dataclasses import dataclass, field

import aiomqtt
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------- 常量 / 契约

ROBOT_COUNT = 100
PUBLISH_INTERVAL_S = 0.1          # 10Hz，100 台 → 全局 ≈ 1000 msg/s
TELEMETRY_TOPIC = "robots/{robot_id}/telemetry"

# spec/00 §5.5 —— 24 个关节标准键名（模拟器/后端/前端三方必须一致）
JOINT_NAMES: tuple[str, ...] = (
    "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw", "left_elbow_pitch",
    "left_wrist_pitch", "left_wrist_roll",
    "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw", "right_elbow_pitch",
    "right_wrist_pitch", "right_wrist_roll",
    "left_hip_yaw", "left_hip_roll", "left_hip_pitch",
    "left_knee_pitch", "left_ankle_pitch", "left_ankle_roll",
    "right_hip_yaw", "right_hip_roll", "right_hip_pitch",
    "right_knee_pitch", "right_ankle_pitch", "right_ankle_roll",
)

# 状态：0 离线 / 1 正常 / 2 预警 / 3 故障（spec/00 §4.1）
STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT = 0, 1, 2, 3

BATTERY_DRAIN_PER_S = 0.05        # spec/04 §3：约 0.05%/s
BATTERY_LOW_THRESHOLD = 15.0      # 低于此值触发 battery_low 告警
OVERHEAT_AFTER_S = 30.0           # 运行 30s 后开始过热升温
OVERHEAT_TARGET_C = 82.0          # 目标温度下限 82°C+
MOTOR_TEMP_ALERT_C = 75.0         # spec/02：>75°C 判定过热

TEMP_BASE_RANGE = (35.0, 65.0)    # 正常关节温度随机游走区间


# ---------------------------------------------------------------- 机器人状态机

@dataclass
class RobotState:
    """单台机器人的状态演化与 payload 生成。"""

    robot_id: str
    battery_pct: float = field(default_factory=lambda: random.uniform(60.0, 100.0))
    joint_temps: dict[str, float] = field(
        default_factory=lambda: {j: random.uniform(*TEMP_BASE_RANGE) for j in JOINT_NAMES}
    )
    overheat_joint: str | None = None      # 被注入过热的关节
    overheating: bool = False
    offline_until: float = 0.0             # monotonic 时间戳，此前静默不发

    # ---- 演化 ----
    def step(self, now: float, start_at: float) -> None:
        """推进一个采样周期（10Hz）的状态。"""
        # 电量单调下降
        self.battery_pct = max(0.0, self.battery_pct - BATTERY_DRAIN_PER_S * PUBLISH_INTERVAL_S)

        # 关节温度随机游走。
        # 注意：过热关节不能套用 65°C 的正常上界，否则每周期都会把爬升中的温度压回 65，
        #       导致永远达不到 82°C+（实测确认，勿再改回统一上界）。
        for j in JOINT_NAMES:
            cur = self.joint_temps[j] + random.uniform(-0.4, 0.4)
            ceiling = TEMP_BASE_RANGE[1]
            if self.overheating and j == self.overheat_joint:
                ceiling = OVERHEAT_TARGET_C + 5.0
            self.joint_temps[j] = min(max(cur, TEMP_BASE_RANGE[0]), ceiling)

        # 过热注入：运行 30s 后，目标关节以每周期 ≤0.6°C 爬升至 82°C+
        if self.overheating and self.overheat_joint and (now - start_at) >= OVERHEAT_AFTER_S:
            j = self.overheat_joint
            cur = self.joint_temps[j]
            if cur < OVERHEAT_TARGET_C:
                self.joint_temps[j] = min(OVERHEAT_TARGET_C + random.uniform(0.0, 3.0),
                                          cur + 0.6)

    def status_and_error(self) -> tuple[int, int]:
        """由状态推导 status_code / error_code。"""
        max_temp = max(self.joint_temps.values())
        if max_temp >= MOTOR_TEMP_ALERT_C:
            return STATUS_FAULT, 1                       # 故障：电机过热
        if self.battery_pct < BATTERY_LOW_THRESHOLD:
            return STATUS_WARN, 2                        # 预警：低电量
        if max_temp >= 70.0:
            return STATUS_WARN, 3                        # 预警：温度偏高
        return STATUS_OK, 0

    def build_payload(self, ts_ms: int) -> dict:
        """构造与 spec/01 §3 对齐的 payload（含预计算 max_joint_temp）。"""
        status, error = self.status_and_error()
        temps = dict(self.joint_temps)
        return {
            "robot_id": self.robot_id,
            "status_code": status,
            "error_code": error,
            "battery_pct": round(self.battery_pct, 2),
            "battery_temp": round(random.uniform(28.0, 45.0), 2),
            "max_joint_temp": round(max(temps.values()), 2),   # 预计算列，必须等于最高关节温度
            "joint_temperatures": {k: round(v, 2) for k, v in temps.items()},
            "joint_positions": {k: round(random.uniform(-1.5, 1.5), 4) for k in JOINT_NAMES},
            "joint_torques": {k: round(random.uniform(0.0, 12.0), 3) for k in JOINT_NAMES},
            "pos_x": round(random.uniform(0.0, 50.0), 3),
            "pos_y": round(random.uniform(0.0, 30.0), 3),
            "pos_z": round(random.uniform(0.0, 3.0), 3),
            "orient_w": round(random.uniform(-1.0, 1.0), 4),
            "orient_x": round(random.uniform(-1.0, 1.0), 4),
            "orient_y": round(random.uniform(-1.0, 1.0), 4),
            "orient_z": round(random.uniform(-1.0, 1.0), 4),
            "ts": ts_ms,
        }


# ---------------------------------------------------------------- 计数器 / 协程

@dataclass
class FleetCounters:
    sent: int = 0
    publish_errors: int = 0
    tps: float = 0.0


async def run_robot(state: RobotState, queue: asyncio.Queue, start_at: float) -> None:
    """单台机器人循环：固定 10Hz 节奏生成 payload 投递到队列；掉线窗口内静默。"""
    next_tick = time.monotonic()
    while True:
        next_tick += PUBLISH_INTERVAL_S
        now = time.monotonic()

        if now < state.offline_until:
            # 掉线注入：不生成也不演化，模拟 MQTT 连接被切断
            await asyncio.sleep(state.offline_until - now)
            next_tick = time.monotonic()
            continue

        state.step(now, start_at)
        await queue.put((TELEMETRY_TOPIC.format(robot_id=state.robot_id),
                         json.dumps(state.build_payload(int(time.time() * 1000)),
                                    separators=(",", ":"))))

        # 对齐 10Hz 节奏，消化 publish/调度耗时带来的漂移
        sleep_for = next_tick - time.monotonic()
        await asyncio.sleep(sleep_for if sleep_for > 0 else PUBLISH_INTERVAL_S)


async def publisher(queue: asyncio.Queue, counters: FleetCounters, broker: str, port: int) -> None:
    """唯一的 MQTT 写入者：串行 publish，断线指数退避重连（上限 30s）。"""
    delay = 1.0
    while True:
        try:
            async with aiomqtt.Client(hostname=broker, port=port) as client:
                delay = 1.0
                while True:
                    topic, payload = await queue.get()
                    await client.publish(topic, payload, qos=0)
                    counters.sent += 1
        except aiomqtt.MqttError:
            counters.publish_errors += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def offline_injector(states: list[RobotState], fault_rate: float) -> None:
    """随机切断某台机器人若干秒，验证后端离线检测。"""
    while True:
        await asyncio.sleep(random.uniform(8.0, 20.0))
        victim = random.choice(states)
        duration = 4.0 + fault_rate * 50.0 + random.uniform(0.0, 4.0)
        victim.offline_until = time.monotonic() + duration


def _fleet_summary(states: list[RobotState], counters: FleetCounters,
                   now: float, start_wall: float) -> str:
    """一行汇总，供 `--no-ui` 模式每秒打印（容器/CI 里没有 rich 面板可刷）。"""
    online = warn = fault = offline = 0
    for s in states:
        status, _ = s.status_and_error()
        if status == STATUS_FAULT:
            fault += 1
        elif status == STATUS_WARN:
            warn += 1
        elif status == STATUS_OFFLINE:
            offline += 1
        else:
            online += 1
    offline += sum(1 for s in states if time.monotonic() < s.offline_until)

    overheated = [s.robot_id for s in states if s.overheating]
    low_battery = sum(1 for s in states if s.battery_pct < BATTERY_LOW_THRESHOLD)

    return (
        f"sent={counters.sent} tps={counters.tps:.0f} "
        f"ok/warn/fault/offline={online}/{warn}/{fault}/{offline} "
        f"overheat={','.join(overheated) if overheated else '-'} "
        f"low_batt={low_battery} pub_err={counters.publish_errors} "
        f"elapsed={now - start_wall:.0f}s"
    )


async def stats_loop(states: list[RobotState], counters: FleetCounters,
                     live: Live | None, start_wall: float) -> None:
    """每秒统计一次并刷新面板 / 打一行日志（唯一的渲染入口）。

    `live is None` 即 `--no-ui`：无 TTY 环境（容器/CI）下 rich 的 Live 控制序列
    会把日志刷成乱码，故降级为 `print` 每秒一行。
    """
    last_sent, last_t = 0, time.monotonic()
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        counters.tps = (counters.sent - last_sent) / (now - last_t)
        last_sent, last_t = counters.sent, now

        online = warn = fault = offline = 0
        for s in states:
            status, _ = s.status_and_error()
            if status == STATUS_FAULT:
                fault += 1
            elif status == STATUS_WARN:
                warn += 1
            elif status == STATUS_OFFLINE:
                offline += 1
            else:
                online += 1
        offline += sum(1 for s in states if time.monotonic() < s.offline_until)

        overheated = [s.robot_id for s in states if s.overheating]
        low_battery = sum(1 for s in states if s.battery_pct < BATTERY_LOW_THRESHOLD)

        if live is None:
            print("[mock_fleet] " + _fleet_summary(states, counters, now, start_wall),
                  flush=True)
            continue

        table = Table.grid(padding=(0, 2))
        table.add_row(Text("累计发送", style="cyan"), f"{counters.sent:,} 条")
        table.add_row(Text("当前 TPS", style="cyan"), f"{counters.tps:.0f} msg/s")
        table.add_row(Text("正常/预警/故障/离线", style="cyan"),
                      f"{online} / {warn} / {fault} / {offline}")
        table.add_row(Text("过热注入中", style="cyan"),
                      ", ".join(overheated) if overheated else "-")
        table.add_row(Text("低电量(<15%)", style="cyan"), str(low_battery))
        table.add_row(Text("publish 失败", style="cyan"), str(counters.publish_errors))
        table.add_row(Text("运行时长", style="cyan"), f"{now - start_wall:.0f}s")
        live.update(Panel(table, title="RobotMonitor 遥测模拟器（100 台 × 10Hz）"))


def pick_overheat_robots(states: list[RobotState], fault_rate: float) -> None:
    """按 fault_rate 选出过热对象；概率命中为 0 时兜底选 1 台，保证告警可被触发。"""
    selected = [s for s in states if random.random() < fault_rate]
    if not selected:
        selected = [random.choice(states)]
    for s in selected:
        s.overheating = True
        s.overheat_joint = random.choice(JOINT_NAMES)


# ---------------------------------------------------------------- 入口

async def amain(args: argparse.Namespace) -> None:
    states = [RobotState(robot_id=f"humanoid-{i:03d}") for i in range(1, args.robots + 1)]
    pick_overheat_robots(states, args.fault_rate)

    counters = FleetCounters()
    queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
    start_wall = time.monotonic()
    console = Console()

    if args.duration and args.duration > 0:
        print(f"[mock_fleet] 受控压测模式：{args.duration}s 后自行退出"
              f"（目标 ≈ {args.robots * 10 * args.duration} 条）", flush=True)

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(publisher(queue, counters, args.broker, args.port)))
    tasks.extend(asyncio.create_task(run_robot(s, queue, start_wall)) for s in states)
    tasks.append(asyncio.create_task(offline_injector(states, args.fault_rate)))

    # 面板与「压测时长」是互斥的两种收尾方式：--no-ui 只是换渲染出口，
    # 而 --duration 决定链路何时被主动掐断（e2e/CI 需要确定性时长）。
    live_ctx = (contextlib.nullcontext(None) if args.no_ui
                else Live(console=console, refresh_per_second=1))
    with live_ctx as live:
        tasks.append(asyncio.create_task(
            stats_loop(states, counters, live, start_wall)))
        try:
            if args.duration and args.duration > 0:
                # 与 stats_loop 的 1s 心跳错开一拍：保证最后一行汇总能落盘
                await asyncio.sleep(args.duration)
                print(f"[mock_fleet] 时长到，停止：sent={counters.sent} "
                      f"(tps≈{counters.sent / args.duration:.0f})", flush=True)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t


def main() -> None:
    parser = argparse.ArgumentParser(description="RobotMonitor 100 台人形机器人遥测模拟器")
    parser.add_argument("--robots", type=int, default=ROBOT_COUNT)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--fault-rate", type=float, default=0.02)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="受控压测秒数；>0 时到点自行退出（0=永久运行，默认）")
    parser.add_argument("--no-ui", action="store_true",
                        help="关闭 rich 面板，改为每秒一行日志（无 TTY 的容器/CI 场景）")
    args = parser.parse_args()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)   # 交由 finally 统一取消任务

    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
