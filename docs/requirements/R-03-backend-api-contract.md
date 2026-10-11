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
| `resolution` | str | `10s` | 支持 `10s` / `30s` / `1m` 等**整秒/整分钟**值（`<n>s` / `<n>m`，n 必须为正整数）；小于 1s、非整数倍（`2.5s` / `1.5m`）、格式非法 一律 400 |

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
| 400 | `resolution` 非法（格式错 / 非整数倍 / <1s） | `{"detail": "invalid resolution: xx"}` |
| 400 | `resolution` 非整数倍（显式语义） | `{"detail": "invalid resolution: 2.5s"}` |
| 503 | 数据库查询失败 | `{"detail": "query failed: ..."}` |

**实现说明（ISSUE #76 修订）**：本端点改查连续聚合 `robot_history_10s`
（`infra/init.sql §5.1`），它**含**三个 JSONB 关节列，刷新策略 end_offset=10s / schedule=30s。

- 原实现直查 `robot_telemetry` 原始表做 `time_bucket`：100 台 × 10Hz ⇒ 5 分钟窗口内
  单台约 3000 行、每次 Drawer 打开都全区间扫描 + 逐行 `last()` 聚合。
- 现改为「先查聚合、空则回退原始表」。回退**必须保留**：
  `WITH NO DATA` 的聚合在首次刷新前是空的，不回退的话冷启动期间 Drawer 曲线恒空。
- **`joint_positions` 在两条分支中都不得删** —— 它是 Drawer 第三条折线「角度(rad)」
  的唯一数据源（订单 16 曾因漏列致曲线恒空，见 STATUS.md §六第 2 条）。
- 数据源有 `robot_backend_history_source_total{source=cagg|raw}` 计数，
  用于把 p95 的下降归因到「真的换了数据源」而非空结果。

### 4.1 `resolution` 解析语义（Issue #74 锁定）

**不得静默截断**。旧实现用 `int(float(...))` 收尾，把 `2.5s` 变成 2 秒、
`1.5m` 变成 60 秒、`0.5m` 变成 1 秒 —— HTTP 200，无任何提示，图上点数与预期对不上。

现行规则（`backend_ingestion/app/main.py:_parse_resolution`）：

| 输入 | 结果 | 说明 |
| :--- | :--- | :--- |
| `10s` / `30s` / `1s` | 10 / 30 / 1 秒 | 整秒，合法 |
| `1m` / `5m` | 60 / 300 秒 | 整分钟，合法（响应回显为 `60s` / `300s`，单位规范化） |
| `2.5s` / `1.5m` / `0.5m` | **400** | 非整数倍，显式拒绝（`resolution must be a whole number of seconds`） |
| `500ms` / `0s` | **400** | 下限 1s（不得放宽，避免压垮查询） |
| `abc` / `10` / 空串 | **400** | 格式非法（必须带 `s`/`m` 单位） |

> 前端 Drawer 仅传 `resolution=10s`（`frontend_dashboard/src/api/robotHistory.ts:13`
> 的 `HISTORY_RESOLUTION`），落在合法区间，本次收紧**不会**触发新的 400。


### 4.1 `resolution` 解析语义（Issue #74 锁定）

**不得静默截断**。旧实现用 `int(float(...))` 收尾，把 `2.5s` 变成 2 秒、
`1.5m` 变成 60 秒、`0.5m` 变成 1 秒 —— HTTP 200，无任何提示，图上点数与预期对不上。

现行规则（`backend_ingestion/app/main.py:_parse_resolution`）：

| 输入 | 结果 | 说明 |
| :--- | :--- | :--- |
| `10s` / `30s` / `1s` | 10 / 30 / 1 秒 | 整秒，合法 |
| `1m` / `5m` | 60 / 300 秒 | 整分钟，合法（响应回显为 `60s` / `300s`，单位规范化） |
| `2.5s` / `1.5m` / `0.5m` | **400** | 非整数倍，显式拒绝（`resolution must be a whole number of seconds`） |
| `500ms` / `0s` | **400** | 下限 1s（不得放宽，避免压垮查询） |
| `abc` / `10` / 空串 | **400** | 格式非法（必须带 `s`/`m` 单位） |

> 前端 Drawer 仅传 `resolution=10s`（`frontend_dashboard/src/api/robotHistory.ts:13`
> 的 `HISTORY_RESOLUTION`），落在合法区间，本次收紧**不会**触发新的 400。


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
