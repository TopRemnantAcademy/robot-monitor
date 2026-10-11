# 订单 03 — 后端高并发采集与存储（核心）

> 派工：`@flow.237/RobotMonitor(后端接入工程师)`
> 顺序：**第 3 单**（依赖 01 基础设施、02 模拟器）
> 依据：`spec/01`、`spec/00` §4 / §5.2 / §5.3 / §5.4 / §5.5、`spec/05` 阶段三

## 交付物（目录严格照 spec/01 §1）
```
backend_ingestion/
├── __init__.py            # ★必须：Docker CMD `backend_ingestion.app.main:app` 依赖它
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── db/{__init__,session,redis,models}.py
│   ├── services/{__init__,mqtt_subscriber,batch_writer,state_cache}.py
│   └── schemas/{__init__,telemetry}.py
├── requirements.txt
├── Dockerfile
└── README.md
```

## 逐文件要求
- `config.py`：pydantic-settings 读取 MQTT_BROKER / MQTT_PORT / REDIS_URL / DATABASE_URL / BATCH_TRIGGER_COUNT / BATCH_TRIGGER_SECONDS。
- `db/models.py`：SQLAlchemy 2.0 Async；`RobotTelemetry` 复合主键 **`(time, robot_id)`**（与 init.sql 一致）；`RobotMetadata`。
- `services/batch_writer.py`：
  - `asyncio.Queue(maxsize=20000)`；
  - **双重触发**：积压 ≥500 条 **或** 距上次写入 >1.0s；
  - 批量写入用 `copy_records_to_table` 或 `execute(insert(...), [...])`，**严禁 for 循环逐条 INSERT**；
  - `asyncio.Lock` 保护刷盘临界区；`graceful_shutdown()` 关闭前强制刷完残留。
- `services/mqtt_subscriber.py`：订阅 `robots/+/telemetry`；捕获 `aiomqtt.MqttError` 指数退避重连（上限 30s）；
  Pydantic 校验失败的消息**丢弃并计数**，不阻塞队列。
- `services/state_cache.py`：写 Redis Hash `robot:state:{robot_id}`（TTL **10s**）；
  发布 `robot:stream:all` 与 `robot:stream:{robot_id}`。
- `schemas/telemetry.py`：Pydantic 校验；`robot_id` 正则 `^humanoid-[0-9]{3}$`；关节字典长度 1~40；`battery_pct` 0~100。

## 接口契约（**缺一即 blocker**）
1. **WS `/ws/robots`** —— 出站必须是 `spec/00 §5.2` 的**带 `type` 信封**：
   - `{"type":"snapshot","data":{"robots":[...]}}` 新连接首帧（100 台，从 Redis Hash 拉取）
   - `{"type":"telemetry","data":{...}}` 增量
   - `{"type":"alert","data":{...}}` ← **必须订阅 Redis `robot:alerts` 并转发**，否则前端告警流永远空白
2. **REST**（前端 Drawer 依赖，缺失则 Drawer 无法工作）：
   - `GET /robots/{robot_id}/history?minutes=5&resolution=10s`
     → `{robot_id, points:[{time,battery_pct,max_joint_temp,joint_temperatures,joint_torques}]}`
     优先查 `robot_status_10s`，聚合未就绪时回退 `robot_telemetry` + `time_bucket`。
   - `GET /robots` → 100 台最新状态
   - `GET /healthz` → 200
3. **生命周期**：startup 启动 MQTT 订阅 + batch_writer 消费 + Redis `robot:alerts` 订阅；shutdown 调 `graceful_shutdown()`。

## Dockerfile
多阶段 `python:3.11-slim`；CMD `uvicorn app.main:app --host 0.0.0.0 --port 8000`（WORKDIR /app/backend_ingestion，2026-10-11 修正，与代码 `from app import ...` 风格一致）。
仓库根 `.dockerignore` 必须排除 `.env`（已存在，不得删除）。

## 验收标准（模拟器已在 02 就绪，可执行）
- [ ] `docker compose up -d` → 启动后端 → 跑模拟器 **30s** → `SELECT count(*) FROM robot_telemetry;` ≈ **30,000**
- [ ] `/healthz` 返回 200
- [ ] WS 首帧为 `type:"snapshot"` 且含 100 台
- [ ] 出现告警时 WS 能收到 `type:"alert"`（可用 redis-cli `PUBLISH robot:alerts '{}'` 手工验证）
- [ ] `GET /robots/humanoid-042/history?minutes=5` 返回非空 points
- [ ] 代码无逐条 INSERT（人工/审查确认）
- [ ] 无占位符；PR 含关键实现 `路径:行号`

## 禁止
- 禁止逐条 INSERT。
- 禁止 WS 裸发对象而不带 `type`。
- 禁止省略 `/robots/{id}/history`。
- 禁止把 `.env` 烘焙进镜像。
