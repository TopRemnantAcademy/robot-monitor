# RobotMonitor 遥测模拟器（订单 02）

在没有 100 台真实机器人时，用本脚本跑满系统性能上限（100 台 × 10Hz ≈ 1000 msg/s），
验证 MQTT 接入、数据库写入压力与告警触发。

## 运行

```bash
pip install -r requirements.txt
python mock_fleet.py --robots 100 --broker localhost --port 1883 --fault-rate 0.02
```

## 参数

| 参数 | 默认 | 说明 |
| :--- | :--- | :--- |
| `--robots` | 100 | 机器人台数（`humanoid-001` ~ `humanoid-100`） |
| `--broker` | localhost | MQTT Broker 地址 |
| `--port` | 1883 | MQTT 端口 |
| `--fault-rate` | 0.02 | 过热注入比例（约 2% 机器人） |

## 三类异常注入

| 类型 | 行为 | 触发的告警规则 |
| :--- | :--- | :--- |
| 电机过热 | 运行 30s 后目标关节以 ≤0.6°C/周期 爬升至 **82°C+** | `motor_temp`（>75°C） |
| 电池耗尽 | 电量按 **0.05%/s** 下降，跌破 **15%** | `battery_low` |
| 随机掉线 | 随机切断某台若干秒（8~20s 间隔） | `offline`（>10s） |

## 契约要点

- **24 个关节键名**严格采用 `spec/00 §5.5` 全名，
  `joint_temperatures` / `joint_positions` / `joint_torques` 共用同一套键。
- payload 与 `spec/01 §3` 对齐，含预计算列 **`max_joint_temp`**（= 最高关节温度）。
- `robot_id` 格式 `humanoid-NNN`。

## 实现说明

- 100 个机器人协程只生成 payload 并投递到 `asyncio.Queue`；
  **由单个 publisher 协程串行 publish** —— `aiomqtt.Client` 不适合多协程并发写，
  并发直接 publish 会产生 "pending publish calls" 堆积。
- 面板用 `rich.live.Live` **每秒刷新一次**，不逐条 print。
- MQTT 断线指数退避重连（1s 起，上限 30s）。

## 任务级协同仿真（Issue #63 / G-7）

`mock_fleet.py` 只产独立波形；`task_sim.py` 在其 payload 构造器与 MQTT 发布器之上
叠加**任务调度器**，让多台机器人遥测在时间上相关（流水线工位轮转 / 故障传播），
相关性完全来自调度逻辑，**不硬编码相关系数**。

```bash
# 流水线场景：纯仿真 + 相关性断言（无需 broker，用于验收）
python simulator/task_sim.py --scenario pipeline --stations 3 --cycle 12 --no-broker --duration 60

# 故障传播场景：发给真实 broker
python simulator/task_sim.py --scenario fault --robots 12 --broker localhost --port 1883
```

| 参数 | 默认 | 说明 |
| :--- | :--- | :--- |
| `--scenario` | pipeline | `pipeline`（流水线工位轮转）/ `fault`（中枢故障→邻居队列积压） |
| `--stations` | 3 | 流水线工位数（pipeline） |
| `--cycle` | 12 | 流水线/故障周期（秒） |
| `--no-broker` | false | 纯仿真 + 相关性断言，不连 MQTT |
| `--out` | task_sim_series.json | 60s 遥测样本导出路径（供绘图/复核） |

验收证据：`--no-broker` 模式导出 60s 遥测并计算 Pearson 相关，断言至少一对机器人
指标序列显著相关（|r| ≥ 0.3），退出码 0=通过 / 2=未达标。实测：pipeline r≈−0.54（相邻工位反相关），
fault r≈+0.99（中枢-邻居正相关）。输出仍走 MQTT（topic/payload 与 mock_fleet 完全一致）。


## AgentVerse 驱动能力探针（Issue #114 / G-7）

G-7 曾计划用 AgentVerse 驱动本模拟器跑 100 台任务级仿真。**实测判定不可行**：
AgentVerse 是 LLM 文本对话框架，`step()` 返回自然语言 `Message`，不产出遥测数值。
探针脚本保留在此，供复现与后续复核：

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

完整实证报告见 [`../docs/agentverse-verification/README.md`](../docs/agentverse-verification/README.md)。
任务级仿真由 `task_sim.py` 承担，其验收测试为 `tests/test_task_sim_harness.py`。
