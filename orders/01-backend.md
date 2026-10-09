# [01] 后端高并发采集与存储服务

## 目标
实现 `backend_ingestion/` 模块：接收 100 台人形机器人 @10Hz 的 MQTT 遥测，写入 Redis（最新状态 Hash + TTL 10s + Pub/Sub 广播），并以异步批量方式写入 TimescaleDB 时序表。

## 依据规范
- `spec/00_system_architecture_spec.md`（架构 + DB Schema + 通信协议）
- `spec/01_backend_data_ingestion_and_storage.md`（目录结构 / 依赖 / 核心逻辑）

## 硬性要求（交给开发 NPC 的指令）
1. 不使用任何 `...` 或占位符，生成完整可运行的 `main.py` / `batch_writer.py` / `mqtt_subscriber.py` / `state_cache.py` / `models.py` / `session.py` / `redis.py` / `config.py` / `telemetry.py`。
2. `batch_writer.py` 必须包含基于 `asyncio.Queue` 的双重触发（满 500 条 **或** 满 1 秒）批量写入 TimescaleDB 逻辑，含 `graceful_shutdown()`。
3. `mqtt_subscriber.py` 必须使用 `aiomqtt` 实现掉线自动重连，并将消息同时推送到 `state_cache` 与 `batch_writer`。
4. `main.py` 必须提供 WebSocket `/ws/robots`（新连接发 100 台快照 + 增量转发）与 `/healthz`。
5. 附带完整多阶段 `Dockerfile`（`python:3.11-slim`）与仓库根 `.dockerignore`（排除 `.env`）。

## 验收标准
- [ ] 无占位符，所有 `.py` 可直接 `import` 运行
- [ ] 后端批量落库：绝不逐条 INSERT（asyncpg 批量 / `execute(insert, [...])`）
- [ ] MQTT 自动重连（指数退避，上限 30s）
- [ ] 跑 `simulator/` 30s，验证 `robot_telemetry` 写入接近 30,000 条
- [ ] `docker compose` 起栈后 `/healthz` 返回 200
- [ ] 通过 PR 提交，PR 描述含关键实现 `路径:行号` 实证

## 全局纪律
遵守根目录 `AGENTS.md`（无幻觉 / 完整可运行 / 性能护栏 / 数据契约 / 安全）。禁止编造依赖或端点；`robot_id` 前缀 `humanoid-`，payload 含 `max_joint_temp` 预计算列。
