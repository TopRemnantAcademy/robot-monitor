# [04] 100 台人形机器人高并发模拟测试器

## 目标
实现 `simulator/mock_fleet.py`：在没有真实硬件时，模拟 100 台人形机器人 @10Hz（全局 1000 Hz）发送 MQTT 遥测，并注入异常以验证告警引擎。

## 依据规范
- `spec/04_mock_robot_telemetry_simulator.md`
- `spec/01_backend_data_ingestion_and_storage.md`（telemetry schema）

## 硬性要求（交给开发 NPC 的指令）
1. 用 `asyncio.gather` 并发调度 100 个协程，支持 `--robots / --broker / --port / --fault-rate` 参数（默认 `--robots 100 --broker localhost --port 1883 --fault-rate 0.02`）。
2. 实现三类异常注入：
   - 电机过热：约 2% 机器人运行 30s 后某关节温度从 45°C 飙升至 82°C+
   - 电池耗尽：电量以约 0.05%/s 消耗，低于 15% 触发低电量
   - 掉线模拟：随机切断某台 MQTT 连接，验证离线检测
3. 用 `rich.live.Live` 显示总发送数 + 当前 TPS + 在线/异常计数面板。
4. payload 必须兼容 spec/01 telemetry schema：字段完整，含 `max_joint_temp` 预计算列，关节命名 24 自由度（左右双肢 + 躯干）。

## 验收标准
- [ ] `python simulator/mock_fleet.py --robots 100` 可运行，TPS≈1000
- [ ] 异常注入生效（可触发过热 / 低电量 / 离线）
- [ ] rich 面板实时刷新不刷屏
- [ ] payload 与 spec/01 schema 一致（含 max_joint_temp）
- [ ] 无占位符；提供 `requirements.txt` 与 `README.md`
- [ ] 通过 PR 提交
