# alert_engine（订单 04）

系统中**唯一做业务判断**的服务：消费后端写入 Redis 的实时状态，基于滑动窗口判定告警，
去重后写入 TimescaleDB `robot_alert` 并 `PUBLISH robot:alerts`。

## 运行

```bash
pip install -r requirements.txt
python -m app.main
```

## 四条规则（缺一不可）

| rule_id | 触发条件 | severity | 去重窗口 |
| :--- | :--- | :--- | :--- |
| `motor_temp` | 任一关节 > **75°C** 且**连续 ≥3 个采样点** | 3 critical | 60s |
| `battery_low` | `battery_pct` < **15%** | 2 warning | 120s |
| `offline` | 无遥测 > **10s**（含从未上报） | 3 critical | 30s |
| `joint_stuck` | 5s 窗内角度方差 < 1e-6 但平均扭矩 > 8.0 | 2 warning | 60s |

## 两条驱动线

1. **消费线** — 订阅 `robot:stream:all`，跑 motor_temp / battery_low / joint_stuck。
2. **ticker 线** — 每秒遍历 100 台跑 offline，并维护 `robot_metadata.is_online / last_seen`。

### 为什么 offline 必须用 ticker

严禁依赖 Redis Key 过期事件：
- Redis 的 keyspace notification（`notify-keyspace-events`）**默认关闭**；
- 且 TTL 到期后 `robot:state:{id}` 已被删除，引擎反而读不到 `last_seen`。

故用**进程内内存字典** `last_seen` 判定；机器人从未上线时 `last_seen is None`，同样判离线。
机器人 ID 全集来自 `config.robot_ids`（100 台，与 `init.sql` 预置一致），
保证"从未上线"的机器人也能被正确判离线。

## 去重

Redis `alert:dedup:{robot_id}:{rule_id}` + `EX`，`SET ... NX` 保证幂等
（同一窗口内重复消息不重复写库）。窗口过期后允许再次触发（恢复后再告警）。

## 关节名

`joint_stuck` 的关节名取自**最新一条遥测的键**，不在此处重复硬编码 24 个名字，
避免与 spec/00 §5.5 / 后端 / 模拟器三处定义漂移。
