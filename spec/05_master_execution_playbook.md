# File 05: AI 编码逐步执行剧本 (Master Prompt Execution Playbook)

> 为防止 AI 一次性接收大量上下文产生「代码截断 / 用 // ... todo 偷懒 / 逻辑脱节」，
> NPC 必须**严格按以下 6 个阶段顺序**执行，每阶段只读取对应的 spec 文件。
>
> **修订说明（重要）**：旧版把「模拟器」放在第 4 阶段，却要求第 2 阶段「用 simulator 跑 30s 验证写入
> 接近 30,000 条」——**依赖倒置，根本无法执行**。现按真实依赖关系重排：
> **先造数据发生器，再写消费方**，保证每个阶段的验收工具在该阶段之前就已存在。

## 依赖关系（为什么是这个顺序）

```
阶段一 基础设施 (compose + init.sql)
   └─> 阶段二 模拟器（只依赖 MQTT Broker，先造出数据源）
         └─> 阶段三 后端（立刻用模拟器验证 30,000 条写入）
               └─> 阶段四 告警引擎（消费后端写入的 Redis 流）
               └─> 阶段五 前端（消费后端 WS + REST）
                     └─> 阶段六 端到端联调
```

---

## 阶段一：基础设施（Infrastructure First）

指令：
> 读取 `spec/00_system_architecture_spec.md`。
> 生成 `infra/docker-compose.yml`、`infra/init.sql`、`backend_ingestion/requirements.txt`。
> 要求：
> - `init.sql` 必须包含 `robot_telemetry` 转 hypertable + `robot_alert` 超表 + 连续聚合定义。
> - `robot_telemetry` 必须有 `PRIMARY KEY (time, robot_id)`（含分区列，TimescaleDB 硬性要求）。
> - `init.sql` 必须**预置 100 台** `robot_metadata`（`humanoid-001` ~ `humanoid-100`）。
> 注意：禁止伪代码；所有 SQL 必须可直接在 TimescaleDB 执行。

**验收**：`docker compose -f infra/docker-compose.yml up -d` 后
`SELECT count(*) FROM robot_metadata;` = 100。

---

## 阶段二：模拟器（Data Source First）

> 先造数据源。模拟器只依赖 MQTT Broker，不依赖后端/告警/前端，因此可以且应当最先完成。

指令：
> 读取 `spec/04_mock_robot_telemetry_simulator.md`。
> 生成 `simulator/mock_fleet.py`（asyncio.gather 100 协程 @10Hz + 三类异常注入 + rich TPS 面板）
> 及 `simulator/requirements.txt`、`simulator/README.md`。
> 关节键名必须严格使用 `spec/00 §5.5` 的 24 个规范键名。

**验收**：`python simulator/mock_fleet.py --robots 100` 可运行，TPS ≈ 1000，
面板显示发送总数且**不刷屏**。

---

## 阶段三：高并发后端与存储（Backend Ingestion）

指令：
> 读取 `spec/01_backend_data_ingestion_and_storage.md`。
> 依次生成：
> 1. `backend_ingestion/__init__.py`（顶层包标记；2026-10-11 起 Docker CMD 为 WORKDIR /app/backend_ingestion + `app.main:app`）
> 2. `app/db/models.py`（SQLAlchemy 模型；`robot_telemetry` 复合主键 `(time, robot_id)`）
> 3. `app/services/batch_writer.py`（核心：异步队列 + 双重触发批量落库 + graceful_shutdown）
> 4. `app/services/mqtt_subscriber.py`（MQTT 接收 + 自动重连）
> 5. `app/services/state_cache.py`（Redis Hash + Pub/Sub）
> 6. `app/main.py`（FastAPI + `/ws/robots` + `/healthz` + `/robots` + `/robots/{id}/history`）
>
> 硬性要求：
> - WS 出站必须是 `spec/00 §5.2` 的**带 `type` 信封**（`snapshot`/`telemetry`/`alert`）。
> - 必须订阅 Redis `robot:alerts` 并转发 `type:"alert"`。
> - 必须提供 REST `/robots/{robot_id}/history`（前端 Drawer 依赖，缺此则 Drawer 无法工作）。

**验收**（此前模拟器已就绪，故可执行）：
`docker compose up -d` → 启动后端 → `python simulator/mock_fleet.py --robots 100` 跑 30s →
`SELECT count(*) FROM robot_telemetry;` 接近 **30,000** 条。

---

## 阶段四：告警引擎（Alerting Engine）

指令：
> 读取 `spec/02_alerting_and_health_analytics_engine.md`。
> 生成 `alert_engine/`（4 个规则类 + Rule 基类 + 去重器 + store + main）。
> - 4 条规则：`motor_temp` / `battery_low` / `offline` / `joint_stuck`，**不可缺 joint_stuck**。
> - `offline` 必须按 `spec/02 §3.1` 用**独立 ticker + 内存 last_seen** 实现，
>   **禁止依赖 Redis Key 过期事件**（keyspace notification 默认关闭）。

**验收**：运行模拟器 30s+，`robot_alert` 中出现 `motor_temp`（过热注入）与 `battery_low` 告警；
手动停掉某台机器人后 10s 内出现 `offline` 告警。

---

## 阶段五：前端看板（Frontend Dashboard）

指令：
> 读取 `spec/03_frontend_dashboard_and_websocket_stream.md`。
> 生成 `frontend_dashboard/`（useRobotWebSocket.ts + FleetOverviewCards + RobotDetailDrawer
> + JointHeatmap + HealthSummaryHeader + AlertLogTable + Zustand store + 类型 + theme tokens）。
> - **必须 100ms 节流**（useRef 缓冲 + setInterval 批量刷 store），绝不每消息重渲染。
> - UI 严格使用 `spec/03 §3` 暗色 token，禁止自由发挥。
> - WS 按 `type` 字段分发：snapshot → 全量填充；telemetry → 增量；alert →  prepend 到告警流。
> - Drawer 用 `GET /robots/{id}/history?minutes=5` 拉历史画 ECharts 折线。

**验收**：`npm install && npm run build` 通过（RC=0）；打开页面可见 100 张卡片、顶栏 KPI、
底部告警流；点击卡片打开 Drawer 且折线有数据。

---

## 阶段六：联调与压力测试（Integration & Stress）

指令：
> 读取 `spec/04` 与 `spec/ci/integration-call.md`。
> 完善根目录 `test_integration.sh`，自动化执行：
> 1. `docker compose -f infra/docker-compose.yml up -d`（EMQX + TimescaleDB + Redis）
> 2. **等待三个组件全部就绪（含 EMQX）**
> 3. 启动 FastAPI 后端，等 `/healthz` = 200
> 4. 启动 alert_engine
> 5. 运行 `python simulator/mock_fleet.py --robots 100` 压测 30s
> 6. 校验 `robot_telemetry` 接近 30,000 条
> 7. 校验 `robot_alert` 触发过热 / 低电量告警

**验收**：脚本可重复运行；区分「环境错误」与「代码错误」；未通过的项标「未验证」并说明所需条件。

---

## 为什么这样分阶段能避免缺陷

1. **依赖正确**：验收工具（模拟器）先于被测对象（后端）产出，不再出现「阶段二要跑模拟器但模拟器在阶段四」。
2. **解耦分块**：每个模块独立 spec 文件，AI 不会因 Token 超限写出 `// ... todo`。
3. **性能护栏**：后端 batch_writer（异步批量落库）+ 前端 100ms 节流，防 1000Hz 冲垮系统。
4. **契约先行**：WS 信封、REST 端点、关节键名在 `spec/00` 统一定义，三方（模拟器/后端/前端）不会各写各的。
