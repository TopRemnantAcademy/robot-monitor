# R-03 后端接口契约（前端 / 联调的唯一事实来源）

> 已由订单 03 实现。本文固化契约，**前端与联调脚本必须以此为准**，不得凭猜测调用。
> 实现位置：`backend_ingestion/app/main.py`

## 1. 通用约定

- 基址：默认 `http://localhost:8000`
- 所有响应 `Content-Type: application/json`
- **无认证**（内网监控系统；若后续加鉴权需同步修订本文）

## 2. `GET /healthz`

健康检查。

```json
{
  "status": "ok",
  "written": 29870,
  "flushes": 62,
  "queue_depth": 0,
  "mqtt_received": 30000,
  "mqtt_dropped": 0
}
```

| 字段 | 含义 | 用途 |
| :--- | :--- | :--- |
| `written` | 已落库总行数 | 联调校验「≈30,000 条」 |
| `queue_depth` | 批量队列积压 | **性能护栏**：持续 > 5000 说明落库跟不上 |
| `mqtt_received` / `mqtt_dropped` | 接收/丢弃计数 | `dropped` 持续增长说明 payload 不合法 |

## 3. `GET /robots`

返回 100 台最新状态（WS 之外的兜底 / 首屏）。

```json
{ "robots": [ { "robot_id": "humanoid-042", "status_code": 1, "error_code": 0,
                "battery_pct": 88.5, "max_joint_temp": 51.2,
                "joint_temperatures": { "...": 45.0 },
                "joint_positions": { "...": 0.1 },
                "joint_torques": { "...": 3.4 }, "ts": 1700000000000 } ] }
```

- 只返回**当前 Redis 中有状态**的机器人（TTL 10s 过期的不返回）。
- 全部离线时返回 `{"robots": []}`（**不是错误**）。

## 4. `GET /robots/{robot_id}/history`

Drawer 的 ECharts 折线与关节热力图数据源。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
| :--- | :--- | :--- | :--- |
| `minutes` | int | 5 | 回溯分钟数 |
| `resolution` | str | `10s` | 支持 `10s` / `30s` / `1m`；小于 1s 返回 400 |

**成功响应**

```json
{
  "robot_id": "humanoid-042",
  "resolution": "10s",
  "points": [
    { "time": "2026-10-10T02:30:00+00:00",
      "status_code": 1, "battery_pct": 88.5, "max_joint_temp": 51.2,
      "joint_temperatures": { "left_knee_pitch": 49.1, "...": 0 },
      "joint_torques": { "left_knee_pitch": 3.4, "...": 0 } }
  ]
}
```

**错误**

| 状态码 | 场景 | 响应体 |
| :--- | :--- | :--- |
| 400 | `resolution` 非法 | `{"detail": "invalid resolution: xx"}` |
| 503 | 数据库查询失败 | `{"detail": "query failed: ..."}` |

**实现说明（重要）**：连续聚合 `robot_status_10s` 不含 JSONB 关节列，
而热力图需要 `joint_temperatures`，故本端点直接对 `robot_telemetry` 原始表做 `time_bucket`，
一次取回标量 + 关节字典。**不要改成只查连续聚合**，否则热力图无数据。

## 5. WebSocket `/ws/robots`

**出站统一信封（强制）**

```jsonc
{"type":"snapshot","data":{"robots":[/* RobotTelemetry */]}}   // 新连接首帧
{"type":"telemetry","data":{/* RobotTelemetry */}}              // 增量
{"type":"alert","data":{"robot_id":"...","rule_id":"...","severity":3,"message":"...","detail":{},"ts":1700000000000}}
```

- 后端订阅 Redis `robot:alerts` 并转发 `type:"alert"` —— **前端告警流的唯一数据源**。
- 数值类型正确（`status_code` 是数字不是字符串）。

## 6. 字段类型契约（易错点）

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `status_code` | **number** | 0 离线 / 1 正常 / 2 预警 / 3 故障 |
| `battery_pct` | number | 0~100 |
| `max_joint_temp` | number | 预计算，等于最高关节温度 |
| `joint_*` | **object** | 键为 24 个标准关节名，值为 number |
| `ts` | number | **毫秒**时间戳 |

> 历史教训：早期版本把 Redis Hash 的字符串原样广播，导致前端收到 `status_code:"1"`，
> 状态色与热力图判断全错。**广播前必须还原类型**。
