# backend_ingestion（订单 03）

100 台人形机器人 10Hz 遥测的高并发采集与存储服务（FastAPI + aiomqtt + asyncpg + Redis）。

## 运行

```bash
pip install -r requirements.txt
# 本地（需先起 infra/docker-compose.yml）
uvicorn app.main:app --host 0.0.0.0 --port 8000
# Docker（注意：CMD 用 backend_ingestion.app.main:app，故 __init__.py 必须存在）
docker build -t robot-monitor-backend ..
```

## 接口契约

| 类型 | 路径 | 说明 |
| :--- | :--- | :--- |
| WS | `/ws/robots` | 首帧 `type:"snapshot"`（100 台），之后 `type:"telemetry"` 增量 + `type:"alert"` 告警 |
| GET | `/robots` | 100 台最新状态（WS 之外兜底） |
| GET | `/robots/{id}/history?minutes=5&resolution=10s` | Drawer 折线 + 关节热力图数据 |
| GET | `/healthz` | 健康检查，含写入计数与队列深度 |

### WS 信封（spec/00 §5.2，强制）

```jsonc
{"type":"snapshot","data":{"robots":[/* RobotTelemetry */]}}
{"type":"telemetry","data":{/* RobotTelemetry */}}
{"type":"alert","data":{"robot_id":"...","rule_id":"...","severity":3,"message":"..."}}
```

> 后端**必须**订阅 Redis `robot:alerts` 并以 `type:"alert"` 转发，否则前端告警流永远为空。

## 性能护栏（不可违反）

- **严禁逐条 INSERT**：`batch_writer` 用 `asyncio.Queue` 双重触发
  （积压 ≥500 条 **或** 距上次刷盘 >1.0s），`asyncpg.executemany` 批量写入。
- `asyncio.Lock` 保护刷盘临界区；`graceful_shutdown()` 关闭前强制刷完残留。
- MQTT 断线指数退避重连（上限 30s），**不改为固定间隔**；`aiomqtt.Client` 实例在重连循环外构造、循环内复用（官方 `reconnection.md`：Client 可复用但不可重入）。
- 依赖 `aiomqtt>=2.5.0`：2.0.1 的 `Client.__aexit__` 在重连场景存在不抛 `MqttError` 的分支，会绕过退避逻辑空转重连（CHANGELOG 2.5.0：`Let exceptions bubble up in Client.__aexit__`）。
- 校验失败的消息**丢弃并计数**，不阻塞队列。

## 关于 /history 的实现说明

`spec/00 §4.3` 的连续聚合 `robot_status_10s` **不含 JSONB 关节列**，
而 Drawer 的关节热力图需要 `joint_temperatures`，
故此处直接对 `robot_telemetry` 原始表做 `time_bucket`，
一次取回标量 + 关节字典，避免二次查询。
