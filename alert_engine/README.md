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
| `offline` | `robot_metadata.is_online = false`（**列不可用时回落**到无遥测 > 10s） | 3 critical | 30s |
| `joint_stuck` | 5s 窗内角度方差 < 1e-6 但平均扭矩 > 8.0 | 2 warning | 60s |

## 两条驱动线

1. **消费线** — 订阅 `robot:stream:all`，跑 motor_temp / battery_low / joint_stuck。
2. **ticker 线** — 每秒遍历 100 台跑 offline。判据**优先读** `robot_metadata.is_online`
   （该列由后端 `metadata_writer` 心跳批量维护，见 ISSUE #96）；该列读不到时回落到
   内存 `last_seen` 时间差判定。本引擎同时按状态变化低频回写该列（#61 既有路径）。

### 为什么 offline 必须用 ticker

严禁依赖 Redis Key 过期事件：
- Redis 的 keyspace notification（`notify-keyspace-events`）**默认关闭**；
- 且 TTL 到期后 `robot:state:{id}` 已被删除，引擎反而读不到 `last_seen`。

故用**独立 ticker** 每轮判定，不依赖 Redis 通知机制。
机器人 ID 全集来自 `config.robot_ids`（100 台，与 `init.sql` 预置一致），
保证"从未上线"的机器人也能被判离线（后端心跳会把它们的 `is_online` 置 false）。

判据优先级（ISSUE #96，见 `app/rules/offline.py`）：
1. `is_online is True` → 不报；`False` → 报（**列即判据**，本进程不再自行推算）；
2. `is_online is None`（列还没被写入 / 本引擎读不到该行）→ **回落**到内存 `last_seen`
   时间差判定，避免部署初期把"列还没写"误报成 100 台离线。
开关：`config.offline_use_is_online`（默认 `True`）。

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

## 验收证据（订单 04 / Issue #4）

本目录有两份**可复跑**的验收物，覆盖 orders/04-alert-engine.md §验收标准：

```bash
# ① 单元级：4 规则触发/不误报、offline 判定的 AST 静态检查、去重幂等、落库+发布
cd alert_engine && python -m pytest tests/test_rules_acceptance.py -q

# ② 端到端级（无 Docker 也能跑）：mock_fleet 的 payload → consume() → 规则 → 去重
#    → store（假连接记 SQL）→ PUBLISH；并驱动真 ticker 验 offline
cd alert_engine && python tests/verify_issue04_acceptance.py
```

② 用到的替身只有三处外部依赖（Redis→fakeredis、PG 连接池→记录器、MQTT→直接调用
`RobotState` 生成 payload），`main.consume` / `main.ticker` / 4 条规则 / `deduplicator` /
`store` 全是真代码。为什么需要它：容器链路（`test_integration.sh`）依赖 Docker，
在无 Docker 环境里「模拟器 30s+ 出告警」「停机 10s 内出 offline」这两条无法在
EMQX+TimescaleDB 里验，但引擎侧逻辑可以完整驱动。

`tests/test_rules_acceptance.py` 另有**反向护栏**：把任一规则/去重器/落库改坏
（阈值取反、去掉 NX、fetchval 换 execute 等），对应用例必须变红——已实测 6/6 全红。
