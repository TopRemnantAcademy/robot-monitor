# File 05: AI 编码逐步执行剧本 (Master Prompt Execution Playbook)

> 为防止 AI 一次性接收大量上下文产生「代码截断 / 用 // ... todo 偷懒 / 逻辑脱节」，
> NPC 必须**严格按以下 4 个阶段顺序**执行，每阶段只读取对应的 spec 文件。

## 阶段一：基础设施与骨架（Infrastructure First）

指令：
> 读取 `spec/00_system_architecture_spec.md`。
> 生成 `infra/docker-compose.yml`、`infra/init.sql`、`backend_ingestion/requirements.txt`。
> 要求：`init.sql` 必须包含 `robot_telemetry` 转 hypertable 语句 + `robot_alert` 超表 + 连续聚合定义。
> 注意：禁止伪代码；所有 SQL 必须可直接在 TimescaleDB 执行。

## 阶段二：高并发后端与存储（Backend Ingestion）

指令：
> 读取 `spec/01_backend_data_ingestion_and_storage.md`。
> 依次生成：
> 1. `app/db/models.py`（SQLAlchemy 模型）
> 2. `app/services/batch_writer.py`（核心：异步队列 + 定时批量落库）
> 3. `app/services/mqtt_subscriber.py`（MQTT 接收 + Redis 缓存）
> 4. `app/services/state_cache.py`（Redis Hash + Pub/Sub）
> 5. `app/main.py`（FastAPI + /ws/robots + /healthz）
> 注意：禁止伪代码；batch_writer 的锁机制与 graceful_shutdown 必须完整。
> 完成后用 `simulator/` 跑 30s，验证 TimescaleDB 写入接近 30,000 条。

## 阶段三：告警引擎与前端看板（Analytics & Frontend）

指令：
> 读取 `spec/02_alerting_and_health_analytics_engine.md` 与 `spec/03_frontend_dashboard_and_websocket_stream.md`。
> 1. 生成 `alert_engine/`（四个规则类 + 去重器 + store + main）。
> 2. 生成 `frontend_dashboard/`（useRobotWebSocket.ts + FleetOverviewCards + RobotDetailDrawer + JointHeatmap + HealthSummaryHeader + AlertLogTable）。
> 注意：前端必须实现 100ms 节流；UI 严格使用 spec/03 §3 暗色 token，禁止自由发挥。

## 阶段四：联调与模拟压力测试（Simulation & Integration）

指令：
> 读取 `spec/04_mock_robot_telemetry_simulator.md`。
> 1. 生成 `simulator/mock_fleet.py`（100 机器人 @10Hz + 异常注入 + rich TPS 面板）。
> 2. 生成根目录 `test_integration.sh`，自动化执行：
>    a. `docker-compose -f infra/docker-compose.yml up -d`（MQTT + TimescaleDB + Redis）
>    b. 启动 FastAPI 后端
>    c. 启动 alert_engine
>    d. 运行 `python simulator/mock_fleet.py --robots 100` 压测 30s
>    e. 查询 TimescaleDB 验证 `robot_telemetry` 写入总条数接近 30,000
>    f. 查询 `robot_alert` 验证过热/低电量告警被触发

## 为什么分阶段能避免缺陷

1. **解耦分块**：每个模块独立 spec 文件，AI 不会因 Token 超限写出 `// ... todo`。
2. **性能护栏**：后端 batch_writer（异步批量落库）+ 前端 100ms 节流，防 1000Hz 冲垮系统。
3. **闭环验证**：File 04 模拟器在无硬件下跑满流量，验证从接入到告警的完整链路。
