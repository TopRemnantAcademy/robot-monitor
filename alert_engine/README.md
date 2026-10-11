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

## 抑制（inhibition）

对照 Alertmanager 的 `inhibit_rules`：`source=offline` / `equal=[robot_id]`。

机器人已判离线（severity 3）时，同一 `robot_id` 的 `motor_temp` / `battery_low` /
`joint_stuck` 告警**不落库、不发布** —— 机器都离线了，「电量低」没有处置价值，根因是离线。

判定在 `main.py` 的 `emit()` 入口，**前置**于去重；实现见 `is_inhibited()`。

三条不变量：

| 不变量 | 原因 |
|---|---|
| `online_state.get(rid) is False`（**不是** falsy 判定） | ticker 跑满一轮前字典为空，`not None` 会把所有告警吞掉 |
| `offline` 自身永不被抑制 | 否则根因告警永远报不出来 |
| 抑制即丢弃，恢复后不补发 | Alertmanager 语义 |

开关：`config.inhibit_offline`（默认 `True`）；设为 `False` 行为回到加抑制之前。
被抑制时打 **DEBUG** 日志，不静默丢弃。

## 去重

Redis `alert:dedup:{robot_id}:{rule_id}` + `EX`，`SET ... NX` 保证幂等
（同一窗口内重复消息不重复写库）。窗口过期后允许再次触发（恢复后再告警）。

## 关节名

`joint_stuck` 的关节名取自**最新一条遥测的键**，不在此处重复硬编码 24 个名字，
避免与 spec/00 §5.5 / 后端 / 模拟器三处定义漂移。

## 落库失败可观测（G-4）

`store.save()` 里 insert 与 PUBLISH 是**两段独立 try/except**，各自失败只记日志。
于是「落库挂了」与「压根没告警」在外部看来完全同形：前端告警流照常滚动（发布还成功），
看板一片绿，只有 DB 对账时才看得出来少数据。

现在 insert 异常分支额外 `metrics.store_errors.inc()`，暴露为：

```
robot_alert_store_errors_total{...}   # Counter，只在写库失败时涨
```

配套规则 `infra/observability/rules/robot-alerts.yml` 的 `AlertStoreFailing`：

```promql
increase(robot_alert_store_errors_total[5m]) > 0     # severity: critical
```

用 `increase` 而不是 `> 0`：Counter 单调，`> 0` 一旦触发就永不复位（永远在响的死警报）；
`increase(...[5m])` 只在"最近 5 分钟又在失败"时响，恢复即静默。

语义提醒：`robot_alert_emitted_total` 涨、`robot_alert_store_errors_total` 也涨
⇒ 告警**确实判定出来了**，只是没进库。这正是看板"假绿"的指纹。

`/metrics` 走 `metrics.start()` 起的独立 HTTP 端口（默认 8001），**无任何鉴权**
（Prometheus 是 pull 模型，加鉴权等于监控静默失效），非 `/metrics` 路径回 404。

验证：

```bash
# 单测（含"把埋点摘掉必须变红"的反向护栏）
cd alert_engine && python -m pytest tests/test_store_metrics.py -q

# 规则指标名 ↔ 代码实存性
bash infra/observability/verify_wiring.sh
```
