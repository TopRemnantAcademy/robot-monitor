# 订单 04 — 告警与健康诊断引擎（4 条规则）

> 派工：`@flow.237/RobotMonitor(告警引擎工程师)`
> 顺序：**第 4 单**（依赖 03 后端产出的 Redis 流）
> 依据：`spec/02`、`spec/00` §2 / §4、`spec/05` 阶段四

## 交付物
```
alert_engine/
├── app/
│   ├── __init__.py
│   ├── main.py          # 订阅 robot:stream:all + 规则调度 + offline ticker
│   ├── config.py        # 阈值/窗口
│   ├── rules/{__init__,base,motor_temp,battery,offline,joint_stuck}.py
│   ├── deduplicator.py
│   └── store.py
├── requirements.txt
└── README.md
```

## 四条规则（缺一不可，spec/00 §2 已同步为 4 条）
| rule_id | 触发 | severity | 去重窗口 |
| :--- | :--- | :--- | :--- |
| `motor_temp` | 任一关节 > **75°C** 且持续 ≥3 采样点 | 3 critical | 60s |
| `battery_low` | `battery_pct` < **15%** | 2 warning | 120s |
| `offline` | 无遥测 > **10s** | 3 critical | 30s |
| `joint_stuck` | 同关节 angle 方差≈0 但 torque 超阈值，持续 **5s** | 2 warning | 60s |

## 关键实现约束
1. 四个规则类继承 `rules/base.py` 的 `Rule` 抽象基类，实现 `evaluate(state, window) -> Alert | None`。
2. **`offline` 必须用独立 ticker 协程 + 内存 `last_seen` 字典**：
   - **严禁依赖 Redis Key 过期事件**——`notify-keyspace-events` 默认关闭，且 TTL 过期后
     `robot:state:{id}` 已被删除，引擎反而读不到 `last_seen`。
   - 机器人 ID 全集从 `robot_metadata` 读取（01 已预置 100 条），保证"从未上线"也能正确判离线。
   - 建议 ticker 每 1s 一轮。
3. `deduplicator.py`：Redis `alert:dedup:{robot_id}:{rule_id}` + `EX`=去重窗口；**必须幂等**，禁止重复写库。
4. `store.py`：写入 `robot_alert` 超表，**并 `PUBLISH robot:alerts`**（前端告警流的唯一数据源）。
5. 滑动窗口用 `collections.deque(maxlen=N)` 维护每台最近 N 条状态。
6. 阈值/窗口全部从 `config.py` 读取。
7. 同时维护 `robot_metadata.is_online` / `last_seen`。

## 验收标准
- [ ] 4 个规则类齐全且继承 Rule 基类（`joint_stuck` 不得遗漏）
- [ ] offline 判定基于 ticker，代码中**不出现**对 Redis 过期事件的依赖
- [ ] 去重器幂等（重复消息不重复写库）
- [ ] 写入 `robot_alert` 且 `PUBLISH robot:alerts` 生效
- [ ] 模拟器跑 30s+ 后出现 `motor_temp`（过热注入）与 `battery_low` 告警
- [ ] 手动停掉某台后 10s 内出现 `offline` 告警
- [ ] 无占位符；PR 含 `路径:行号`

## 禁止
- 禁止只实现 3 条规则（spec/00 §2 旧版曾漏写 joint_stuck，现已修正为 4 条）。
- 禁止用 Redis TTL 过期事件判离线。
- 禁止绕过 deduplicator 直接写库。
