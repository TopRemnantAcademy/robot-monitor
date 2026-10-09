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
