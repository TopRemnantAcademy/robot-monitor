# File 01: 后端高并发数据采集与存储服务规范 (Ingestion & Storage Specification)

> 严格遵循 `00_system_architecture_spec.md`。本模块接收 100 台人形机器人 10Hz 遥测，
> 写入 Redis（最新状态 + Pub/Sub），并异步批量写入 TimescaleDB。

## 1. 工程目录结构（强制）

```
backend_ingestion/
├── __init__.py               # 必须存在：Docker CMD 用 `backend_ingestion.app.main:app` 导入本包
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI 入口 + 生命周期钩子 + /ws/robots
│   ├── config.py             # pydantic-settings 环境变量
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py        # AsyncPG / SQLAlchemy 异步连接池
│   │   ├── redis.py          # Async Redis 连接池与 Hash 操作
│   │   └── models.py         # SQLAlchemy ORM 模型
│   ├── services/
│   │   ├── __init__.py
│   │   ├── mqtt_subscriber.py# aiomqtt 异步订阅（自动重连）
│   │   ├── batch_writer.py   # 队列缓冲 + 批量刷盘引擎
│   │   └── state_cache.py    # Redis 最新状态写入 + Pub/Sub 广播
│   └── schemas/
│       ├── __init__.py
│       └── telemetry.py      # Pydantic 校验 Schema
├── requirements.txt
├── Dockerfile
└── README.md
```

## 2. 依赖版本（锁定，`requirements.txt`）

```
fastapi==0.110.0
uvicorn[standard]==0.28.0
pydantic==2.6.4
pydantic-settings==2.2.1
asyncpg==0.32.0
sqlalchemy[asyncio]==2.0.28
redis[hiredis]==5.0.3
aiomqtt==2.5.1
uvloop==0.19.0; sys_platform != 'win32'
websockets==12.0
prometheus-client==0.21.1
```

> `prometheus-client` 由订单 13 接线（`GET /metrics`）。规范里的版本清单必须与
> `backend_ingestion/requirements.txt` 逐行一致，否则又是一次「文档与契约漂移」。

## 3. 数据校验 Schema (`app/schemas/telemetry.py`)

- `robot_id`: 字符串，正则 `^humanoid-[0-9]{3}$`（本系统用 `humanoid-001` 格式；模拟器若用 `robot_001` 需在订阅端做映射，AI 实现时统一为 `humanoid-` 前缀）。
- `status_code`: int（0 离线 / 1 正常 / 2 预警 / 3 故障）。
- `error_code`: int，默认 0。
- `battery_pct`: float，范围 `0.0 ~ 100.0`。
- `joint_temperatures` / `joint_positions` / `joint_torques`: dict，键为关节名，值 float；**字典长度 1~40**。
- `pos_x/y/z`, `orient_w/x/y/z`: 可选 float（姿态四元数）。
- 校验失败的消息**丢弃并计数**（不阻塞队列）。

## 4. 核心实现细节

### 4.1 ORM 模型 (`app/db/models.py`)

SQLAlchemy 2.0 Async 模式：
- `RobotTelemetry`: 映射 `robot_telemetry`（`time` + `robot_id` 复合主键，对应 00 的 Schema）。
- `RobotMetadata`: 映射 `robot_metadata`（见 00 §4.2）。

### 4.2 批量写入引擎 (`app/services/batch_writer.py`)

- 内部 `asyncio.Queue(maxsize=20000)` 缓冲池。
- **双重触发**：队列积压 ≥ `BATCH_TRIGGER_COUNT`(500) 条，**或** 距上次写入 > `BATCH_TRIGGER_SECONDS`(1.0s)，触发刷盘。
- 刷盘使用 `asyncpg` 底层 `copy_records_to_table` 或 `execute(insert(...), [...])` 批量插入，**严禁 for 循环逐条 INSERT**。
- `graceful_shutdown()`: 应用关闭时把队列残留全部强制刷盘。
- 用 `asyncio.Lock` 保护刷盘临界区，避免并发刷盘。

### 4.3 实时缓存与 Pub/Sub (`app/services/state_cache.py`)

- 最新状态写入 Redis Hash：`robot:state:{robot_id}`，TTL = 10s（断连后自动过期）。
- 同时写入预计算标量 `max_joint_temp` 供连续聚合（见 00 §4.3 说明）。
- 发布到 Redis Channel：`robot:stream:all`（全量）与 `robot:stream:{robot_id}`（单机）。

### 4.4 MQTT 订阅 (`app/services/mqtt_subscriber.py`)

- `aiomqtt.Client` 订阅 `robots/+/telemetry`。
- **自动断线重连**：捕获 `aiomqtt.MqttError` 后指数退避重连（上限 30s）。
- 收到消息 → Pydantic 校验 → 同时投递给 `state_cache` 与 `batch_writer` 队列。

### 4.5 入口、WebSocket 与 REST (`app/main.py`)

- FastAPI 生命周期：`startup` 启动 MQTT 订阅任务 + batch_writer 消费任务 + Redis `robot:alerts` 订阅任务；
  `shutdown` 调 `graceful_shutdown()`。
- WebSocket 路由 `/ws/robots`：新连接发送 100 台最新快照（从 Redis Hash 拉取），之后订阅 Redis
  `robot:stream:all` 转发增量。
- **必须订阅 Redis `robot:alerts` 并以 `{"type":"alert","data":{...}}` 转发**（见 `spec/00 §5.2`），
  否则前端告警流永远为空。
- **REST 路由（Drawer 依赖，不可省略）**：
  - `GET /robots/{robot_id}/history?minutes=5&resolution=10s` → 最近 N 分钟历史点（优先查
    `robot_status_10s`，聚合未就绪时回退 `robot_telemetry` + `time_bucket`）。
  - `GET /robots` → 100 台最新状态（WS 之外兜底）。
  - `GET /healthz` → 200。`status` 反映依赖可达性（`ok`/`degraded`），
    并暴露 `db` / `retries` / `rejected` 与丢行三元组
    `rows_submitted` / `rows_written` / `rows_skipped`（最后一项是
    `ON CONFLICT DO NOTHING` 静默丢弃的行数，即「收到的数据没全部入库」的直接证据）。
  - `GET /metrics` → 200，`text/plain; version=0.0.4`（Prometheus exposition format）。
- 所有 WS 出站消息必须是 `spec/00 §5.2` 定义的**带 `type` 的信封**（`snapshot` / `telemetry` / `alert`）。

## 5. 多阶段 Dockerfile（基于 `python:3.11-slim`）

```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
COPY backend_ingestion /app/backend_ingestion
EXPOSE 8000
CMD ["uvicorn", "backend_ingestion.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

> 必须在仓库根目录提供 `.dockerignore`，排除 `.env`、`__pycache__`、`.git`，**严禁把密钥烘焙进镜像**（见 00 §6）。

## 6. AI 编写指令（交给 NPC 的 prompt）

> "请严格根据 `spec/00_system_architecture_spec.md` 与 `spec/01_backend_data_ingestion_and_storage.md`
> 编写完整的 Python FastAPI 遥测采集系统。
> 硬性要求：
> 1. 不使用任何 `...` 或占位符，生成完整可运行的 main.py / batch_writer.py / mqtt_subscriber.py / models.py / state_cache.py / session.py / redis.py / config.py / telemetry.py。
> 2. batch_writer.py 必须包含基于 asyncio.Queue 的双重触发（满 500 条或满 1 秒）批量写入 TimescaleDB 逻辑，含 graceful_shutdown。
> 3. mqtt_subscriber.py 必须使用 aiomqtt 实现掉线自动重连，并将消息同时推送到 state_cache 与 batch_writer。
> 4. main.py 必须提供 /ws/robots WebSocket 与 /healthz。
> 5. 附带完整多阶段 Dockerfile（python:3.11-slim）与仓库根 .dockerignore。"
