#!/usr/bin/env python3
"""RobotMonitor telemetry simulator: 100 humanoid robots @10Hz MQTT telemetry + fault injection.

Spec references
---------------
- backend_ingestion/app/schemas/telemetry.py (24 joint standard key names, single source of truth)
- backend_ingestion/app/schemas/api.py (payload schema, section 3)

Design notes (performance & correctness)
----------------------------------------
1. 100 robot coroutines **only generate payloads** and push them to a single `asyncio.Queue`;
   a **single publisher coroutine** serially performs MQTT publish.
   Reason: `aiomqtt.Client` is not suited to concurrent writes from many coroutines; 100
   coroutines publishing directly causes "pending publish calls" to pile up (confirmed in
   practice). After serialization, throughput is stable at ~1000 msg/s.
2. Three fault injections: overheat (82°C+)/ battery depletion (<15%)/ random disconnect.
3. `rich.live.Live` refreshes the summary panel **once per second** — never prints line by
   line (that would flood the terminal).

`--duration N`: exits on its own after N seconds (for controlled e2e/CI load tests; see tests/e2e/).
`--no-ui`: turns off the rich panel and uses a one-line-per-second plain log instead; without a
TTY in a container, Live degrades to per-frame log spam, so disabling the panel keeps
`docker compose logs` readable (the e2e harness uses this flag).
`--min-sent M`: on exit, if `sent < M`, prints the reason and exits with a **non-zero** code.
Why this is needed — `--duration` only guarantees "ran for N seconds", not "actually sent how
many". A wrong broker address, robot count silently overridden by an env var, or persistent
publish-side errors all let the simulator exit 0 peacefully, pushing the failure to the
assertion stage or past the threshold check (silent fake-green). A lower bound makes the load
test fail red first.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import signal
import sys
import time
from dataclasses import dataclass, field

import aiomqtt
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------- constants / contract

ROBOT_COUNT = 100
PUBLISH_INTERVAL_S = 0.1          # 10Hz; 100 robots -> global ~1000 msg/s
TELEMETRY_TOPIC = "robots/{robot_id}/telemetry"
# R-VLA-EVAL Phase 1: parallel VLA policy-action topic (one action frame per
# control tick, same frequency as telemetry).
ACTION_TOPIC = "robots/{robot_id}/action"
# Synthetic policy identity (stands in for a trained checkpoint until a real VLA
# node is wired in via rosbag2/rosetta).
POLICY_VERSION = "vla-sim-0.1"
ACTION_DIM = 6                    # 6-DoF end-effector delta (x,y,z,rx,ry,rz)

# telemetry.py (24 joint standard key names) — simulator/backend/frontend must all agree
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

# Status: 0 offline / 1 ok / 2 warning / 3 fault (telemetry.py status_code contract)
STATUS_OFFLINE, STATUS_OK, STATUS_WARN, STATUS_FAULT = 0, 1, 2, 3

BATTERY_DRAIN_PER_S = 0.05        # ~0.05%/s
BATTERY_LOW_THRESHOLD = 15.0      # below this triggers a battery_low alert
OVERHEAT_AFTER_S = 30.0           # start heating up after 30s of running
OVERHEAT_TARGET_C = 82.0          # target temperature floor 82°C+
MOTOR_TEMP_ALERT_C = 75.0         # >75°C judged as overheat

TEMP_BASE_RANGE = (35.0, 65.0)    # normal joint-temperature random-walk range


# ---------------------------------------------------------------- timestamp issuance

def _next_ts_ms(last_ms: int | None, now_ms: int) -> int:
    """Issue a timestamp for one sample, guaranteeing **strictly monotonic per-robot**
    (millisecond granularity).

    Why an issuer is needed, instead of `int(time.time() * 1000)` per sample
    ----------------------------------------------------------------
    The primary key of `robot_telemetry` is `(time, robot_id)`, where `time` comes from the
    payload `ts` (`backend_ingestion/app/services/batch_writer.py:_to_row()`).
    So "one robot emits two rows in the same millisecond" = PK conflict =
    `ON CONFLICT DO NOTHING` **silently drops the row**, surfacing only in `rows_skipped`.

    This really happens under "backlog replay", not just in theory:

    1. `run_robot`'s cadence is `next_tick += 0.1` + `sleep(next_tick - now)`; **on overrun it
       does not wait, the due time still lands in the past**; while the `publisher` stalls on
       MQTT jitter (`aiomqtt.MqttError` -> exponential-backoff reconnect, up to 30s),
       `run_robot` keeps pushing into the `asyncio.Queue`; after reconnect the backlog drains fast;
    2. during drain, `run_robot`'s loop body runs many iterations in **far less than 100ms** real
       time, each calling `int(time.time() * 1000)` once -> the same millisecond is sampled many
       times. Measured locally: sampling 200 times at 0.5ms intervals within one millisecond
       yielded only 111 unique milliseconds; across 10 runs of the 100-robot × 10Hz load test,
       one run hit "same (ts, robot_id) appears twice".

    Issuance rule (both properties must hold simultaneously; see tests/test_ts_sampling.py)
    -------------------------------------------------------------
    - **strictly increasing**: `next > last`, eliminating duplicates (a duplicate is a PK-conflict
      dropped row);
    - **follows real time**: when there is no backlog `next == now`, so `time` is not pushed away
      from the real collection instant (otherwise "last 60s" time-window queries would mismatch
      the data).
    Only on conflict (a second arrival in the same millisecond) do we nudge +1 to the next ms.

    Args:
        last_ms: this robot's previous millisecond timestamp; pass None for the first row.
        now_ms: the wall-clock millisecond timestamp for this sample.

    Returns:
        The millisecond timestamp to emit this time, guaranteed > last_ms.
    """
    if last_ms is None or now_ms > last_ms:
        return now_ms
    return last_ms + 1


# ---------------------------------------------------------------- robot state machine

@dataclass
class RobotState:
    """State evolution and payload generation for a single robot."""

    robot_id: str
    battery_pct: float = field(default_factory=lambda: random.uniform(60.0, 100.0))
    joint_temps: dict[str, float] = field(
        default_factory=lambda: {j: random.uniform(*TEMP_BASE_RANGE) for j in JOINT_NAMES}
    )
    overheat_joint: str | None = None      # the joint injected with overheat
    overheating: bool = False
    offline_until: float = 0.0             # monotonic timestamp; silent before this

    # ---- evolution ----
    def step(self, now: float, start_at: float) -> None:
        """Advance one sampling cycle (10Hz) of state."""
        # Battery drains monotonically
        self.battery_pct = max(0.0, self.battery_pct - BATTERY_DRAIN_PER_S * PUBLISH_INTERVAL_S)

        # Joint temperature random walk.
        # NOTE: the overheated joint must NOT be clamped to the normal 65°C ceiling, otherwise
        # every cycle would push the climbing temperature back to 65 and it would never reach
        # 82°C+ (confirmed in practice; do not revert to a unified ceiling).
        for j in JOINT_NAMES:
            cur = self.joint_temps[j] + random.uniform(-0.4, 0.4)
            ceiling = TEMP_BASE_RANGE[1]
            if self.overheating and j == self.overheat_joint:
                ceiling = OVERHEAT_TARGET_C + 5.0
            self.joint_temps[j] = min(max(cur, TEMP_BASE_RANGE[0]), ceiling)

        # Overheat injection: after 30s of running, the target joint climbs at <=0.6°C per cycle
        # toward 82°C+.
        if self.overheating and self.overheat_joint and (now - start_at) >= OVERHEAT_AFTER_S:
            j = self.overheat_joint
            cur = self.joint_temps[j]
            if cur < OVERHEAT_TARGET_C:
                self.joint_temps[j] = min(OVERHEAT_TARGET_C + random.uniform(0.0, 3.0),
                                          cur + 0.6)

    def status_and_error(self) -> tuple[int, int]:
        """Derive status_code / error_code from state."""
        max_temp = max(self.joint_temps.values())
        if max_temp >= MOTOR_TEMP_ALERT_C:
            return STATUS_FAULT, 1                       # fault: motor overheat
        if self.battery_pct < BATTERY_LOW_THRESHOLD:
            return STATUS_WARN, 2                        # warning: low battery
        if max_temp >= 70.0:
            return STATUS_WARN, 3                        # warning: temperature high
        return STATUS_OK, 0

    def build_payload(self, ts_ms: int) -> dict:
        """Build the payload aligned with api.py (section 3), incl. the precomputed max_joint_temp."""
        status, error = self.status_and_error()
        temps = dict(self.joint_temps)
        return {
            "robot_id": self.robot_id,
            "status_code": status,
            "error_code": error,
            "battery_pct": round(self.battery_pct, 2),
            "battery_temp": round(random.uniform(28.0, 45.0), 2),
            "max_joint_temp": round(max(temps.values()), 2),   # precomputed column; must equal the max joint temp
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

    def build_action(self, ts_ms: int) -> dict:
        """Build a synthetic VLA policy-action frame (R-VLA-EVAL Phase 1).

        Stands in for an external policy node's output: a 6-DoF end-effector
        delta command per tick. Mirrors the PolicyAction contract in
        backend_ingestion/app/schemas/telemetry.py (robot_id / policy_version /
        trial_id / action / goal / ts). trial_id/goal are None during normal
        operation; they are populated by the A/B trial layer (Phase 2+).
        """
        return {
            "robot_id": self.robot_id,
            "policy_version": POLICY_VERSION,
            "trial_id": None,
            "action": [round(random.uniform(-0.1, 0.1), 4) for _ in range(ACTION_DIM)],
            "goal": None,
            "ts": ts_ms,
        }


# ---------------------------------------------------------------- counters / coroutines

@dataclass
class FleetCounters:
    sent: int = 0
    publish_errors: int = 0
    tps: float = 0.0


async def run_robot(state: RobotState, queue: asyncio.Queue, start_at: float) -> None:
    """Single-robot loop: generate payload at a fixed 10Hz cadence and push to the queue; silent during disconnect windows."""
    next_tick = time.monotonic()
    last_ts_ms: int | None = None
    while True:
        next_tick += PUBLISH_INTERVAL_S
        now = time.monotonic()

        if now < state.offline_until:
            # Disconnect injection: neither generate nor evolve; simulate the MQTT link cut
            await asyncio.sleep(state.offline_until - now)
            next_tick = time.monotonic()
            continue

        state.step(now, start_at)
        # Timestamp issuance goes through _next_ts_ms, **not** a per-sample `int(time.time()*1000)`:
        # that would, under "backlog replay", make one robot emit two rows in the same millisecond,
        # hitting robot_telemetry's (time, robot_id) PK -> ON CONFLICT DO NOTHING silently drops rows.
        # See _next_ts_ms's docstring and tests/test_ts_sampling.py.
        ts_ms = _next_ts_ms(last_ts_ms, int(time.time() * 1000))
        last_ts_ms = ts_ms
        await queue.put((TELEMETRY_TOPIC.format(robot_id=state.robot_id),
                         json.dumps(state.build_payload(ts_ms),
                                    separators=(",", ":"))))
        # R-VLA-EVAL Phase 1: emit a parallel VLA policy-action frame. Reuses the
        # same ts_ms so the two streams share the (time, robot_id) cadence; both
        # go through the same single publisher and are independently validated on
        # the backend (a bad action payload is dropped without touching telemetry).
        await queue.put((ACTION_TOPIC.format(robot_id=state.robot_id),
                         json.dumps(state.build_action(ts_ms),
                                    separators=(",", ":"))))

        # Re-align to the 10Hz cadence, absorbing drift from publish/scheduling cost
        sleep_for = next_tick - time.monotonic()
        await asyncio.sleep(sleep_for if sleep_for > 0 else PUBLISH_INTERVAL_S)


async def publisher(queue: asyncio.Queue, counters: FleetCounters, broker: str, port: int) -> None:
    """The sole MQTT writer: serial publish, exponential-backoff reconnect on disconnect (cap 30s)."""
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
    """Randomly cut one robot's link for a few seconds, to verify backend offline detection."""
    while True:
        await asyncio.sleep(random.uniform(8.0, 20.0))
        victim = random.choice(states)
        duration = 4.0 + fault_rate * 50.0 + random.uniform(0.0, 4.0)
        victim.offline_until = time.monotonic() + duration


def _fleet_summary(states: list[RobotState], counters: FleetCounters,
                   now: float, start_wall: float) -> str:
    """One-line summary, printed once per second in `--no-ui` mode (no rich panel to refresh in containers/CI)."""
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
    """Tally once per second and refresh the panel / print one log line (the only render entry point).

    `live is None` means `--no-ui`: without a TTY (container/CI), rich's Live control sequences
    garble the log, so we degrade to `print` one line per second.
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
        table.add_row(Text("Total sent", style="cyan"), f"{counters.sent:,} rows")
        table.add_row(Text("Current TPS", style="cyan"), f"{counters.tps:.0f} msg/s")
        table.add_row(Text("ok/warn/fault/offline", style="cyan"),
                      f"{online} / {warn} / {fault} / {offline}")
        table.add_row(Text("Overheating", style="cyan"),
                      ", ".join(overheated) if overheated else "-")
        table.add_row(Text("Low battery (<15%)", style="cyan"), str(low_battery))
        table.add_row(Text("publish errors", style="cyan"), str(counters.publish_errors))
        table.add_row(Text("Runtime", style="cyan"), f"{now - start_wall:.0f}s")
        live.update(Panel(table, title="RobotMonitor telemetry simulator (100 robots x 10Hz)"))


def pick_overheat_robots(states: list[RobotState], fault_rate: float) -> None:
    """Pick overheat targets by fault_rate; if the probability hits 0, fall back to 1 robot to keep the alert triggerable."""
    selected = [s for s in states if random.random() < fault_rate]
    if not selected:
        selected = [random.choice(states)]
    for s in selected:
        s.overheating = True
        s.overheat_joint = random.choice(JOINT_NAMES)


# ---------------------------------------------------------------- entry point

async def amain(args: argparse.Namespace) -> int:
    states = [RobotState(robot_id=f"humanoid-{i:03d}") for i in range(1, args.robots + 1)]
    pick_overheat_robots(states, args.fault_rate)

    counters = FleetCounters()
    queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
    start_wall = time.monotonic()
    console = Console()

    if args.duration and args.duration > 0:
        print(f"[mock_fleet] Controlled load-test mode: exits after {args.duration}s "
              f"(target ~{args.robots * 10 * args.duration} rows)", flush=True)

    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(publisher(queue, counters, args.broker, args.port)))
    tasks.extend(asyncio.create_task(run_robot(s, queue, start_wall)) for s in states)
    tasks.append(asyncio.create_task(offline_injector(states, args.fault_rate)))

    # The panel and "load-test duration" are two mutually exclusive endings: --no-ui only swaps
    # the render outlet, while --duration decides when the chain is actively cut (e2e/CI needs a
    # deterministic duration).
    live_ctx = (contextlib.nullcontext(None) if args.no_ui
                else Live(console=console, refresh_per_second=1))
    with live_ctx as live:
        tasks.append(asyncio.create_task(
            stats_loop(states, counters, live, start_wall)))
        try:
            if args.duration and args.duration > 0:
                # Offset by one beat from stats_loop's 1s heartbeat: guarantee the final summary lands
                await asyncio.sleep(args.duration)
                print(f"[mock_fleet] Duration reached, stopping: sent={counters.sent} "
                      f"(tps~{counters.sent / args.duration:.0f})", flush=True)
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    # The send-volume self-check runs after finally: regardless of normal or cancelled ending, report honestly.
    if args.min_sent > 0 and counters.sent < args.min_sent:
        print(f"[mock_fleet] Send volume below target: sent={counters.sent} < --min-sent="
              f"{args.min_sent} (publish failed {counters.publish_errors} times). "
              "Common causes: unreachable broker/wrong port, robot count overridden by env var, "
              "persistent publish-side errors.",
              flush=True)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="RobotMonitor 100-humanoid-robot telemetry simulator")
    parser.add_argument("--robots", type=int, default=ROBOT_COUNT)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--fault-rate", type=float, default=0.02)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Controlled load-test seconds; >0 exits when reached (0=run forever, default)")
    parser.add_argument("--no-ui", action="store_true",
                        help="Disable the rich panel, print one log line per second (container/CI without a TTY)")
    parser.add_argument("--min-sent", type=int, default=0,
                        help="Send-volume floor; >0 exits non-zero if final sent is below it (0=no floor, default)")
    args = parser.parse_args()

    if args.min_sent < 0:
        parser.error("--min-sent cannot be negative")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)   # unified task cancellation in finally

    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
