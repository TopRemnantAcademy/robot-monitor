# File 04: 100 台人形机器人高并发模拟测试器规范 (Telemetry Simulator)

> 在没有 100 台真实物理机器人的情况下，用本脚本跑满系统性能上限、验证数据库写入压力与告警触发。

## 1. 模块定位

- 模拟规模：100 台人形机器人（`humanoid-001` ~ `humanoid-100`）。
- 发送频率：每台每 100ms 一次（10Hz），全局 1000 Hz。
- 异常注入（Fault Injection）：用于验证告警引擎。

## 2. 目录与文件

```
simulator/
├── mock_fleet.py          # 主模拟器（asyncio + aiomqtt）
├── requirements.txt       # aiomqtt, rich
└── README.md
```

## 3. 核心逻辑

- 关节命名（24 自由度，左右双肢 + 躯干）：
  `shoulder_p/r/y, elbow, wrist_p/r, hip_y/r/p, knee, ankle_p/r`（左右各一套）。
- 每台机器人维护：电量（随时间消耗）、各关节温度（正常 35~65°C 随机游走）。
- **异常注入**：
  - 电机过热：约 2% 机器人运行 30s 后某关节温度从 45°C 飙升至 82°C+。
  - 电池耗尽：电量以约 0.05%/s 消耗，低于 15% 触发低电量事件。
  - 掉线模拟：随机切断某台 MQTT 连接，验证离线检测。
- payload 字段与 `spec/01` 的 `telemetry.py` schema 完全一致（含 `max_joint_temp`）。

## 4. 控制台面板（rich）

- 动态显示：已发送总 Payload 数、当前 TPS（每秒事务数）、在线/异常机器人计数。
- 使用 `rich.live.Live` 实时刷新，不刷屏。

## 5. 命令行参数

```
python mock_fleet.py --robots 100 --broker localhost --port 1883 --fault-rate 0.02
```

## 6. AI 编写指令（交给 NPC 的 prompt）

> "请根据 `spec/04_mock_robot_telemetry_simulator.md` 完善 `simulator/mock_fleet.py`。
> 1. 用 asyncio.gather 并发调度 100 个协程，支持 `--robots / --broker / --port / --fault-rate` 参数。
> 2. 实现 §3 的三类异常注入（过热/低电量/掉线）。
> 3. 用 rich 库显示 TPS 与总发送数面板。
> 4. payload 必须兼容 spec/01 的 telemetry schema（含 max_joint_temp 预计算列）。
> 5. 不使用占位符；提供 requirements.txt 与 README。"
