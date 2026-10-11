# File 00: 系统总体架构与数据库 Schema 规范 (System Architecture & DB Schema)

> 本文档是整套系统的**全局蓝图**，所有后续模块（01~04）都必须严格遵循本文定义的数据结构、
> 通信协议与数据库 Schema。AI 生成代码时，凡是与本文冲突的，以本文为准。

## 1. 业务范围与监控目标（Scope）

本系统只做**基础运维 2D 看板**（不做 3D 数字孪生 / CAD 渲染）：

- 100 台人形机器人（`humanoid-001` ~ `humanoid-100`）同时在工厂作业。
- 每台机器人 20~40 个关节（典型 24 自由度的左右双肢 + 躯干）。
- 监控指标：**在线/离线状态、电量、电机/关节温度、扭矩、姿态坐标、故障码**。
- 频率：每台 10Hz，全局 1000 条/秒。
- 不做：CAD/URDF 3D 渲染、SLAM 地图、碰撞检测（这些留待后续阶段）。

## 2. 系统四层架构

```
[ 100 台人形机器人 (Edge Agent: Zenoh-Bridge-ROS2 / paho-mqtt) ]
        │  Wi-Fi 6 工业局域网, MQTT topic: robots/{robot_id}/telemetry
        ▼
[ 数据采集层 Ingestion (backend_ingestion, FastAPI) ]
        ├─ MQTT 订阅 (aiomqtt, 自动重连)
        ├─ asyncio.Queue 缓冲池 → 批量写入 TimescaleDB
        └─ Redis Hash 最新状态 (TTL 10s) + Redis Pub/Sub (robot:stream:all)
        │
        ▼ (WebSocket /ws/robots)
[ 前端看板 (frontend_dashboard, React + Vite + Tailwind + ECharts) ]
        ├─ 100 台机器人卡片墙 (FleetOverviewCards)
        ├─ 顶部全局指标 (HealthSummaryHeader)
        ├─ 单机关节热力诊断 Drawer (RobotDetailDrawer)
        └─ 底部实时告警流 (AlertLogTable)
        │
        ▼
[ 告警引擎 (alert_engine) ]
        ├─ 滑动窗口四条规则：电机过热(>75°C) / 低电量(<15%) / 离线(>10s) / 关节卡死
        ├─ Redis 状态去重，避免重复告警
        └─ 告警写入 TimescaleDB + 经 Redis `robot:alerts` 由后端 WS 转发到前端告警流
```

> 注意：告警引擎共有 **4 条**规则（与 `spec/02` 一致），`关节卡死` 不可遗漏。

## 3. 中间件与基础设施（docker-compose）

| 组件 | 镜像 | 端口 | 用途 |
| :--- | :--- | :--- | :--- |
| EMQX | `emqx:5.6` | 1883 (MQTT), 18083 (Dashboard) | 高并发 MQTT Broker，接收 100 台机器人数据 |
| TimescaleDB | `timescale/timescaledb:2.13.1-pg16` | 5432 | 时序数据库，存储遥测 + 元数据 |
| Redis | `redis:7.2-alpine` | 6379 | 最新状态缓存 + Pub/Sub 广播 |

详见 `infra/docker-compose.yml`。

## 4. 数据库 Schema（TimescaleDB / PostgreSQL）

### 4.1 `robot_telemetry` 时序表（核心高频表）

```sql
CREATE TABLE robot_telemetry (
    time            TIMESTAMPTZ NOT NULL,
    robot_id        TEXT        NOT NULL,
    status_code     INTEGER     NOT NULL,   -- 1:正常 2:预警 3:故障 0:离线
    error_code      INTEGER     NOT NULL DEFAULT 0,
    battery_pct     DOUBLE PRECISION NOT NULL,
    battery_temp    DOUBLE PRECISION,
    joint_temperatures JSONB    NOT NULL,   -- {"joint_0_shoulder_p": 42.1, ...}
    joint_positions     JSONB    NOT NULL,  -- {"joint_0_shoulder_p": 0.12, ...}
    joint_torques       JSONB    NOT NULL,  -- {"joint_0_shoulder_p": 3.4, ...}
    pos_x           DOUBLE PRECISION,
    pos_y           DOUBLE PRECISION,
    pos_z           DOUBLE PRECISION,
    orient_w        DOUBLE PRECISION,
    orient_x        DOUBLE PRECISION,
    orient_y        DOUBLE PRECISION,
    orient_z        DOUBLE PRECISION,
    PRIMARY KEY (time, robot_id)   -- 必须包含分区列 time（TimescaleDB 硬性要求）
);
-- 转为超表，按时间分块（7 天一块）
SELECT create_hypertable('robot_telemetry', 'time', chunk_time_interval => INTERVAL '7 days');
```

### 4.2 `robot_metadata` 元数据表（低频，随配置变更）

```sql
CREATE TABLE robot_metadata (
    robot_id         TEXT PRIMARY KEY,
    model_type       TEXT,
    firmware_version TEXT,
    assigned_station TEXT,
    is_online        BOOLEAN DEFAULT FALSE,
    last_seen        TIMESTAMPTZ
);
```

### 4.3 连续聚合（Continuous Aggregate）—— 供前端看板秒级刷新

```sql
-- 每 10 秒聚合一台机器人的最新状态（在线率 / 平均电量 / 最高关节温度）
CREATE MATERIALIZED VIEW robot_status_10s
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('10 seconds', time) AS bucket,
    robot_id,
    last(status_code, time)  AS status_code,
    last(battery_pct, time)  AS battery_pct,
    max(joint_temperatures)  AS max_joint_temperatures  -- 应用层解析 JSONB 取最高温
FROM robot_telemetry
GROUP BY bucket, robot_id;

SELECT add_continuous_aggregate_policy('robot_status_10s',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
```

> 说明：`max(joint_temperatures)` 在 JSONB 上不成立，AI 实现时改为在 `batch_writer` 落库前
> 预计算标量列 `max_joint_temp DOUBLE PRECISION`，连续聚合对 `max(max_joint_temp)` 取最大值。
> 本文保留 JSONB 形态以兼容 schema，落地以预计算标量列为准。

## 5. 通信协议（MQTT → WebSocket / REST）

### 5.1 机器人 → Broker

MQTT topic `robots/{robot_id}/telemetry`，JSON payload（见 `spec/01` 的 `telemetry.py` schema）。

### 5.2 后端 → 前端：WebSocket（**统一信封格式，强制**）

端点 `ws://{server}:8000/ws/robots`。

> **必须带 `type` 字段的信封**：早期版本只说「单台最新状态 / 批量快照」，导致前端无法区分
> **遥测**与**告警**，告警流永远为空。现强制统一为以下信封：

```jsonc
// ① 首次连接：100 台全量快照
{ "type": "snapshot", "data": { "robots": [ { /* RobotTelemetry */ }, ... ] } }

// ② 增量遥测（单台，10Hz/台，经 100ms 节流后由前端批量消费）
{ "type": "telemetry", "data": { /* RobotTelemetry */ } }

// ③ 告警（由 alert_engine 产生，经 Redis `robot:alerts` 转发）
{ "type": "alert", "data": { "robot_id": "...", "rule_id": "...", "severity": 3, "message": "...", "ts": 1712... } }
```

- 后端必须订阅 Redis `robot:alerts` 并以 `type:"alert"` 转发，否则前端告警流无数据源。
- `RobotTelemetry` / `RobotAlert` 字段见 `spec/03 §7` 类型定义（本文档与之对齐）。

### 5.3 后端 → 前端：REST（**单机历史，Drawer 依赖**）

> `spec/03 §5.3` 的 Detail Drawer 需要「最近 5 分钟历史」画 ECharts 折线，
> 但早期后端只定义了 WS，没有 REST → **Drawer 无法工作**。现补齐：

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| `GET` | `/robots/{robot_id}/history?minutes=5&resolution=10s` | 返回该机器人最近 N 分钟遥测（默认 5 分钟 / 10s 分辨率），供 Drawer 折线与热力图 |
| `GET` | `/robots` | 返回 100 台机器人最新状态（WS 之外的兜底/首屏） |
| `GET` | `/healthz` | 健康检查，返回 200 |

`/history` 返回结构：

```jsonc
{ "robot_id": "humanoid-042",
  "points": [ { "time": "...", "battery_pct": 88.1, "max_joint_temp": 51.2,
                "joint_temperatures": { "...": 42.1 }, "joint_torques": { "...": 3.4 } } ] }
```

数据来源优先 `robot_history_10s` 连续聚合（ISSUE #76 新增，含 JSONB 关节列，
刷新 end_offset=10s / schedule=30s）；聚合未就绪时回退查 `robot_telemetry` 原始表并做 `time_bucket`。

> 修订（ISSUE #76）：原稿写「优先 `robot_status_10s`」，但该视图**不含** JSONB 关节列，
> Drawer 热力图与第三条折线需要它们，落地时被迫一直走原始表 —— 于是「优先连续聚合」
> 这句话在实现里从未成立。现新增 `robot_history_10s`（与 `robot_status_10s` 并存，
> 后者仍服务看板最新态），使「优先连续聚合」第一次真正落地。
> 视图定义见 `infra/init.sql §5.1`，含 JSONB 的可行性依据（`last()` 为 anyelement 多态）。

### 5.4 后端内部

Redis Pub/Sub `robot:stream:all`（全量遥测）与 `robot:stream:{robot_id}`（单机），
以及 `robot:alerts`（告警），供水平扩展的 WS 服务订阅。

### 5.5 关节命名（**唯一事实来源，三方必须一致**）

24 自由度，左右各一套。模拟器生成、后端入库 JSONB 的键、前端热力图渲染必须**全部使用下列键名**：

```
left_shoulder_pitch,  left_shoulder_roll,  left_shoulder_yaw,  left_elbow_pitch,
left_wrist_pitch,     left_wrist_roll,
right_shoulder_pitch, right_shoulder_roll, right_shoulder_yaw, right_elbow_pitch,
right_wrist_pitch,    right_wrist_roll,
left_hip_yaw,   left_hip_roll,   left_hip_pitch,  left_knee_pitch,  left_ankle_pitch,  left_ankle_roll,
right_hip_yaw,  right_hip_roll,  right_hip_pitch, right_knee_pitch, right_ankle_pitch, right_ankle_roll
```

共 24 个关节。`joint_temperatures` / `joint_positions` / `joint_torques` 三个 JSONB 必须使用同一套键。

## 6. 环境变量（`.env` 模板，禁止提交真实密钥）

```
MQTT_BROKER=localhost
MQTT_PORT=1883
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/robot_monitor
BATCH_TRIGGER_COUNT=500
BATCH_TRIGGER_SECONDS=1.0
```

## 7. 全局工程纪律（所有模块必须遵守）

1. **禁止伪代码 / 占位符**：所有生成的 `.py` / `.tsx` / `.ts` 必须是完整可运行文件，不允许出现 `...`、`// TODO`、`pass` 占位（除空函数体的合理实现）。
2. **性能护栏**：后端必须批量落库（不逐条 INSERT）；前端必须 100ms 节流（不每消息重渲染）。
3. **file:line 实证**：NPC 提交代码时，PR 描述必须包含关键实现点的 `file:line` 引用。
4. **无幻觉依赖**：只使用本文锁定版本的依赖，禁止编造未存在的库 / 类名 / API 端点。
5. **闭环验证**：任何模块完成后，必须能用 `04` 模拟器跑通端到端链路。
