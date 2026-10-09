# 订单 02 — 100 台遥测模拟器（数据源先行）

> 派工：`@flow.237/RobotMonitor(后端接入工程师)`
> 顺序：**第 2 单**（必须先于后端产出 —— 后端要靠它验收）
> 依据：`spec/04`、`spec/00` §5.5（关节键名）、`spec/01` §3（payload schema）、`spec/05` 阶段二

## 为什么排在第 2
旧版把模拟器放最后，却要求"后端阶段用模拟器验证 30,000 条"——依赖倒置，无法执行。
模拟器**只依赖 MQTT Broker**，是唯一能独立产出的数据源，故必须先造。

## 交付物
```
simulator/
├── mock_fleet.py        # 主模拟器
├── requirements.txt     # aiomqtt, rich
└── README.md
```

## 实现要求
1. **并发**：`asyncio.gather` 调度 100 个协程（`humanoid-001` ~ `humanoid-100`），每台 10Hz（每 100ms 一发）。
2. **CLI**：`--robots`（默认 100）`--broker`（默认 localhost）`--port`（1883）`--fault-rate`（0.02）。
3. **关节键名（强制，唯一事实来源 spec/00 §5.5，共 24 个）**：
   ```
   left_shoulder_pitch,  left_shoulder_roll,  left_shoulder_yaw,  left_elbow_pitch,
   left_wrist_pitch,     left_wrist_roll,
   right_shoulder_pitch, right_shoulder_roll, right_shoulder_yaw, right_elbow_pitch,
   right_wrist_pitch,    right_wrist_roll,
   left_hip_yaw,  left_hip_roll,  left_hip_pitch,  left_knee_pitch,  left_ankle_pitch,  left_ankle_roll,
   right_hip_yaw, right_hip_roll, right_hip_pitch, right_knee_pitch, right_ankle_pitch, right_ankle_roll
   ```
   `joint_temperatures` / `joint_positions` / `joint_torques` 三个字典**必须使用同一套键**。
4. **payload 字段**（对齐 spec/01 §3）：
   `robot_id`（`^humanoid-[0-9]{3}$`）、`status_code`、`error_code`、`battery_pct`、
   `joint_temperatures` / `joint_positions` / `joint_torques`、`pos_x/y/z`、`orient_w/x/y/z`、
   **`max_joint_temp`（必须预计算 = max(joint_temperatures.values())）**。
5. **三类异常注入**：
   - 电机过热：约 2% 机器人运行 30s 后某关节温度由 45°C 升到 **82°C+**（触发 motor_temp）。
   - 电池耗尽：电量按约 **0.05%/s** 消耗，跌破 **15%**（触发 battery_low）。
   - 掉线：随机断开某台 MQTT 连接一段时间（触发 offline）。
6. **面板**：`rich.live.Live` 显示累计发送数、当前 TPS、在线/异常计数，**不刷屏**。

## 验收标准
- [ ] `python simulator/mock_fleet.py --robots 100` 可运行，TPS ≈ **1000**
- [ ] 24 个关节键名与 spec/00 §5.5 完全一致（逐键比对，非数量比对）
- [ ] payload 含 `max_joint_temp` 且值等于最高关节温度
- [ ] 三类异常注入均能实际触发（不是只写逻辑）
- [ ] rich 面板实时刷新不刷屏
- [ ] 无占位符；PR 含 `路径:行号`

## 禁止
- 禁止使用 `robot_001` 之类 ID（必须 `humanoid-` 前缀）。
- 禁止自创关节名（例如 `shoulder_p` 简写），必须用 §5.5 全名。
- 禁止逐条 print（会冲垮终端）。
