# RobotMonitor telemetry simulator (Order 02)

When there are no 100 real robots, use this script to drive the system to its performance ceiling (100 robots × 10Hz ≈ 1000 msg/s), validating MQTT ingestion, DB write pressure, and alert triggering.

## Running

```bash
pip install -r requirements.txt
python mock_fleet.py --robots 100 --broker localhost --port 1883 --fault-rate 0.02
```

## Parameters

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `--robots` | 100 | number of robots (`humanoid-001` ~ `humanoid-100`) |
| `--broker` | localhost | MQTT broker address |
| `--port` | 1883 | MQTT port |
| `--fault-rate` | 0.02 | overheat injection ratio (~2% of robots) |

## Three kinds of fault injection

| Type | Behavior | Alert rule triggered |
| :--- | :--- | :--- |
| Motor overheat | after 30s of running, the target joint climbs at ≤0.6°C/cycle to **82°C+** | `motor_temp` (>75°C) |
| Battery drain | battery drops at **0.05%/s**, falling below **15%** | `battery_low` |
| Random disconnect | randomly cut a robot off for several seconds (8~20s interval) | `offline` (>10s) |

## Contract highlights

- The **24 joint key names** strictly follow `spec/00 §5.5` full names; `joint_temperatures` / `joint_positions` / `joint_torques` share the same key set.
- The payload aligns with `spec/01 §3`, including the precomputed column **`max_joint_temp`** (= highest joint temperature).
- `robot_id` format `humanoid-NNN`.

## Implementation notes

- The 100 robot coroutines only generate payload and deliver it to `asyncio.Queue`; **a single publisher coroutine publishes serially** — `aiomqtt.Client` is not suited to concurrent writes from many coroutines; concurrent publish leads to a pile-up of "pending publish calls".
- The panel uses `rich.live.Live` to **refresh once per second**, not printing per row.
- MQTT disconnect uses exponential-backoff reconnect (starts at 1s, cap 30s).

## Task-level coordinated simulation (Issue #63 / G-7)

`mock_fleet.py` only produces independent waveforms; `task_sim.py` layers a **task scheduler** on top of its payload constructor and MQTT publisher, making multiple robots' telemetry temporally correlated (assembly-line station rotation / fault propagation). The correlation comes entirely from the scheduling logic, **with no hardcoded correlation coefficients**.

```bash
# 流水线场景：纯仿真 + 相关性断言（无需 broker，用于验收）
python simulator/task_sim.py --scenario pipeline --stations 3 --cycle 12 --no-broker --duration 60

# 故障传播场景：发给真实 broker
python simulator/task_sim.py --scenario fault --robots 12 --broker localhost --port 1883
```

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `--scenario` | pipeline | `pipeline` (assembly-line station rotation) / `fault` (hub fault → neighbor queue backlog) |
| `--stations` | 3 | number of assembly-line stations (pipeline) |
| `--cycle` | 12 | assembly-line/fault period (seconds) |
| `--no-broker` | false | pure simulation + correlation assertion, no MQTT connection |
| `--out` | task_sim_series.json | 60s telemetry sample export path (for plotting/review) |

Acceptance evidence: the `--no-broker` mode exports 60s of telemetry and computes the Pearson correlation, asserting that at least one pair of robot metric series is significantly correlated (|r| ≥ 0.3); exit code 0=pass / 2=fail. Measured: pipeline r≈−0.54 (adjacent-station anti-correlation), fault r≈+0.99 (hub-neighbor positive correlation). Output still goes over MQTT (topic/payload identical to mock_fleet).

## AgentVerse drive-capability probe (Issue #114 / G-7)

G-7 once planned to use AgentVerse to drive this simulator for a 100-robot task-level simulation. **Measured verdict: not feasible**: AgentVerse is an LLM text-dialog framework; its `step()` returns natural-language `Message`s and produces no telemetry values. The probe script is kept here for reproduction and future review:

```bash
# 前置：clone AgentVerse（物料不随本仓交付）+ 装依赖（aliyun 镜像）
git clone --depth 1 https://github.com/OpenBMB/AgentVerse /tmp/agentverse_probe
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    numpy "openai==1.1.0" "httpx==0.25.0" colorama colorlog \
    py3langid "tiktoken==0.5.1" rapidfuzz "langchain==0.0.157" scikit-learn astunparse

# 三探针：import gate / 构造 100 agent / step 产物是否命中遥测契约
python simulator/agentverse_probe/verify_agentverse.py --av-root /tmp/agentverse_probe

# 对照实验：给一个可用的本地 LLM，看是否就产遥测（结论：仍不产）
bash simulator/agentverse_probe/verify_with_local_llm.sh
```

Full empirical report: [`../docs/agentverse-verification/README.md`](../docs/agentverse-verification/README.md).
The task-level simulation is handled by `task_sim.py`; its acceptance test is `tests/test_task_sim_harness.py`.
