# File 02: 告警与健康诊断引擎规范 (Alerting & Health Analytics Engine)

> 本模块是系统中**唯一做业务判断**的服务。它消费后端推送的实时状态，基于滑动窗口规则
> 判定电机过热 / 低电量 / 离线 / 关节卡死，去重后写入 TimescaleDB `robot_alert` 表，
> 并通过 Redis Pub/Sub `robot:alerts` 推送给前端告警流。

## 1. 工程目录结构

```
alert_engine/
├── app/
│   ├── __init__.py
│   ├── main.py               # 入口：订阅 robot:stream:all + 调度判定循环
│   ├── config.py             # pydantic-settings
│   ├── rules/
│   │   ├── __init__.py
│   │   ├── base.py           # Rule 抽象基类（evaluate 返回 Alert 或 None）
│   │   ├── motor_temp.py     # 电机过热规则
│   │   ├── battery.py        # 低电量规则
│   │   ├── offline.py        # 离线检测规则
│   │   └── joint_stuck.py    # 关节卡死规则
│   ├── deduplicator.py       # Redis 状态去重，避免重复告警
│   └── store.py              # 告警写入 TimescaleDB + 发布 robot:alerts
├── requirements.txt
└── README.md
```

## 2. 告警数据模型（落库 `robot_alert` 表）

```sql
CREATE TABLE robot_alert (
    time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    robot_id    TEXT        NOT NULL,
    rule_id     TEXT        NOT NULL,   -- 'motor_temp' | 'battery_low' | 'offline' | 'joint_stuck'
    severity    INTEGER     NOT NULL,   -- 1:info 2:warning 3:critical
    message     TEXT        NOT NULL,
    detail      JSONB,                  -- 例如 {"joint":"joint_9_knee","temp":82.3}
    ack         BOOLEAN DEFAULT FALSE
);
SELECT create_hypertable('robot_alert', 'time');
```

## 3. 判定规则（阈值与滑动窗口）

| 规则 | 触发条件 | severity | 去重窗口 |
| :--- | :--- | :--- | :--- |
| `motor_temp` | 任一关节温度 > **75°C**（持续 ≥ 3 个采样点） | 3 critical | 60s |
| `battery_low` | `battery_pct` < **15%** | 2 warning | 120s |
| `offline` | 距本进程记录的 `last_seen` > **10s** 无任何遥测 | 3 critical | 30s |
| `joint_stuck` | 同一关节 `angle` 连续 **5s** 方差 ≈ 0 但 `torque` > 阈值 | 2 warning | 60s |

- 滑动窗口在内存用 `collections.deque(maxlen=N)` 维护每台机器人最近 N 条状态。
- 阈值与窗口全部从 `config.py` 读取，便于调参。

### 3.1 `offline` 规则的判定方式（**必须按此实现**）

> **禁止依赖 Redis Key 过期事件**：早期文档写「Redis Hash TTL 过期即判定」，但 Redis 的
> keyspace notification（`notify-keyspace-events`）**默认关闭**，依赖过期事件会导致离线告警永不触发。
> 且 TTL 过期后 `robot:state:{id}` 已被删除，引擎反而读不到 `last_seen`。

正确实现：
1. 引擎在收到每条 `robot:stream:all` 消息时，在内存字典里更新 `last_seen[robot_id] = now()`。
2. 另起一个**独立的 ticker 协程**（建议每 1s 一轮），遍历全部 100 台机器人
   （ID 列表来自 `robot_metadata`，已由 `init.sql` 预置 100 条），
   若 `now() - last_seen[robot_id] > 10s` 则触发 `offline` 告警。
3. 这样完全不依赖 Redis 配置，且机器人从未上线时也能正确判定为离线。

## 4. 去重器 (`deduplicator.py`)

- 用 Redis Key `alert:dedup:{robot_id}:{rule_id}`，SET 时带 `EX` = 去重窗口秒数。
- 同一 `(robot_id, rule_id)` 在窗口内只产生一条告警；窗口过期后允许下一次触发（恢复后再告警）。
- 必须幂等：重复消息不重复写库。

## 5. 消费与分发 (`main.py` + `store.py`)

- 启动后订阅 Redis `robot:stream:all`（由后端 `state_cache` 发布）。
- 每条消息更新对应机器人的滑动窗口，遍历所有 Rule 调用 `evaluate`。
- `evaluate` 返回 Alert → 经 `deduplicator` 过滤 → `store` 写入 `robot_alert` 并 `PUBLISH robot:alerts`。
- 同时维护 `robot_metadata.is_online` / `last_seen`（离线/恢复时更新）。

## 6. AI 编写指令（交给 NPC 的 prompt）

> "请严格根据 `spec/00` 与 `spec/02_alerting_and_health_analytics_engine.md` 编写告警引擎。
> 硬性要求：
> 1. 四个规则类（motor_temp / battery / offline / joint_stuck）必须继承 `rules/base.py` 的 Rule 抽象基类，实现 `evaluate(state, window) -> Alert | None`。
> 2. deduplicator.py 必须用 Redis Key + EX 实现按 (robot_id, rule_id) 去重，禁止重复写库。
> 3. main.py 必须订阅 `robot:stream:all`，维护每台机器人滑动窗口，并驱动规则判定循环。
> 4. store.py 必须把告警写入 TimescaleDB `robot_alert` 超表，并 PUBLISH 到 `robot:alerts`。
> 5. 所有阈值/窗口从 config.py 读取；不使用占位符；提供 requirements.txt 与 README。"
