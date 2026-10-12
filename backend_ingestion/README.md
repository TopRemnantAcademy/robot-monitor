# backend_ingestion（订单 03）

100 台人形机器人 10Hz 遥测的高并发采集与存储服务（FastAPI + aiomqtt + asyncpg + Redis）。

## 运行

```bash
pip install -r requirements.txt
# 本地（需先起 infra/docker-compose.yml）
uvicorn app.main:app --host 0.0.0.0 --port 8000
# Docker（CMD 与本地一致：WORKDIR /app/backend_ingestion + app.main:app）
docker build -t robot-monitor-backend ..
```

## 接口契约

| 类型 | 路径 | 说明 |
| :--- | :--- | :--- |
| WS | `/ws/robots` | 首帧 `type:"snapshot"`（100 台），之后 `type:"telemetry"` 增量 + `type:"alert"` 告警 |
| GET | `/robots` | 100 台最新状态（WS 之外兜底） |
| GET | `/robots/{id}/history?minutes=5&resolution=10s` | Drawer 折线 + 关节热力图数据 |
| GET | `/healthz` | 健康检查：`status` 反映依赖可达性（`ok`/`degraded`），含 `db` / `retries` / `rejected` / 丢行三元组 `rows_submitted|rows_written|rows_skipped` |
| GET | `/metrics` | Prometheus 文本格式（`text/plain; version=0.0.4`），供 `infra/observability/prometheus.yml` 的 `robot_backend` job 抓取 |

### WS 信封（spec/00 §5.2，强制）

```jsonc
{"type":"snapshot","data":{"robots":[/* RobotTelemetry */]}}
{"type":"telemetry","data":{/* RobotTelemetry */}}
{"type":"alert","data":{"robot_id":"...","rule_id":"...","severity":3,"message":"..."}}
```

> 后端**必须**订阅 Redis `robot:alerts` 并以 `type:"alert"` 转发，否则前端告警流永远为空。

## 性能护栏（不可违反）

- **严禁逐条 INSERT**：`batch_writer` 用 `asyncio.Queue` 双重触发
  （积压 ≥500 条 **或** 距上次刷盘 >1.0s）批量写入。
- `asyncio.Lock` 保护刷盘临界区；`graceful_shutdown()` 关闭前强制刷完残留（靠 `_STOP` 哨兵
  唤醒阻塞在 `queue.get()` 的 `run()`，**不能用 cancel**，否则最后一批在飞数据会丢）。
- **刷盘必须有硬超时**：本机对 PostgreSQL 15.19 实测，表被另一会话
  `LOCK TABLE ... ACCESS EXCLUSIVE` 占住时，批量写入会挂起且**无视** `command_timeout`
  （连接级设 5s 也照样挂 >25s）。挂住的后果是采集链路静默停摆，
  故刷批统一加 `asyncio.wait_for` 上限：宁可丢一批 + 打 error 日志，也不堵死消费循环。
- 计数出口：`/healthz` 给瞬时快照，`/metrics` 给 Counter/Gauge（Prometheus 才能算 `rate()`）。

### robot_metadata 心跳（G-6 / ISSUE #96）

`robot_metadata.is_online / last_seen` 的批量写方是
`app/services/metadata_writer.py`，从 `state_cache.write_and_publish()` 投递
（与 `liveness.record()` 同一时点）。它遵守同一套护栏：

- **按 robot_id 折叠**：1000 msg/s（100 台 × 10Hz）在 1s 窗口内只折算成 ≤100 行；
- **单语句批量**：`UPDATE ... FROM unnest($1::text[], $2::timestamptz[])`，
  1000 条遥测 → **恰好 1 次**库往返（不是 executemany、更不是 for 循环）；
- **`last_seen` 取服务端时钟**：不取 payload 的 `ts`（客户端时钟偏差 / 重放 / 离线缓发
  会让同轮离线扫描把它刚写 TRUE 的行立刻翻回 FALSE —— 实测踩过）；
- 开关 `METADATA_HEARTBEAT_ENABLED=false` 时 `run()` 直接退出、`flush()` 不发语句，
  既有遥测写路径的语义与吞吐不受影响。
- 计数出口：`/healthz` 的 `metadata` 字段（`flushes` / `rows_written` / `offline_marks` / `deduped`）。

## 写入降级链（写路径的核心设计）

spec/01 §4.2 允许 `copy_records_to_table` **或** 批量 `execute`。本机实测两者吞吐都在
70k~200k 行/s，相对 1000 行/s 峰值有两个数量级冗余 —— **吞吐不是选型依据，容错才是**。
实测出的三个硬事实决定了实现必须分级：

1. `copy_records_to_table` **不支持 `ON CONFLICT`**，重复批次抛 `UniqueViolationError`；
   而重试恰恰会重复提交同一批 → 幂等必须靠临时表中转。
2. COPY 传 `dict` 会 `TypeError: expected str, got dict` → JSONB 列必须传 JSON 文本。
3. **单行坏数据连坐整批**（COPY / executemany 都是批级原子）。

故实现三级降级链（见 `app/services/batch_writer.py`）：

| 级 | 路径 | 特性 |
| :-- | :--- | :--- |
| L1 | COPY → 临时表 → `INSERT ... SELECT ... ON CONFLICT DO NOTHING` | 快、**幂等**、批级原子 |
| L2 | `executemany(INSERT ... ON CONFLICT DO NOTHING)` | 慢一档的兜底 |
| L3 | 二分隔离坏行 | 坏行丢进 `rejected`，**好数据保住** |

> 为什么必须有 L3：一批 500 条时，一行脏 payload 若不隔离就等于每 0.5 秒丢 500 条遥测。

`/healthz` 与 `/metrics` 暴露的计数口径：

| 字段 | 含义 | 口径注意 |
| :--- | :--- | :--- |
| `retries` | 重试次数 | 持续增长说明 DB 不稳 |
| `rejected` | 被 L3 判定不可写而丢弃的坏行数 | **非 0 就是丢数据** |
| `rows_submitted` | 提交给落库的行数 | 三者的分子 |
| `rows_written` | 真正入库的行数 | L1 走 `INSERT 0 N` 的 affected rows |
| `rows_skipped` | 被 `ON CONFLICT DO NOTHING` 静默丢弃的行数 | = `submitted - written` |

> 注：`feat/order-08` / `fix/issue19-merge-safe` 的分支版本里有一个 `flush_failures`
> 计数器，**main 版本没有，本次也没有新增**（未定义即不写，避免自造指标）。

> `rows_skipped` 是「收到的数据没全部入库」的**直接证据**（ISSUE #19 G2）。
> 此前 `written += len(rows)` 计的是**提交行数**，静默丢行完全不可见 —— 行数偏少时只能猜。
> 本机实测（PostgreSQL 15.19）：10 行提交两次 → `submitted=20 / written=10 / skipped=10`，
> 与库内真实 10 行一致。
>
> **已知口径缺口**：L2 路径的 `executemany` 不回传逐行 affected rows，
> 故走 L2 时 `rows_skipped` 恒为 0。量化以 L1 为准；L2 只在 L1 连续失败后启用（罕见）。
- MQTT 断线指数退避重连（上限 30s），**不改为固定间隔**；`aiomqtt.Client` 实例在重连循环外构造、循环内复用（官方 `reconnection.md`：Client 可复用但不可重入）。
- 依赖 `aiomqtt==2.5.1`。此前写过「2.0.1 的 `Client.__aexit__` 吞异常导致重连不执行」，
  **该结论已撤稿**（方向性错误）：实测 v2.0.1→v2.5.0 源码 diff，2.0.1 是 `raise exc`，
  2.5.0 反而**删掉了**它。两个版本重连都能工作，真实差异是 2.0.1 会二次抛出并掩盖原始栈。
  证据见 `external/ORDER12-VERIFICATION.md` §2。
- 校验失败的消息**丢弃并计数**，不阻塞队列。

## 连接池（`app/db/session.py`）

`asyncpg==0.32.0` 把 `min_size` 从「建池时预建几条」改成了「空闲时的**连接地板**」，
原来的预建语义挪给新参数 `init_size`。本服务的读路径（`/healthz` 探活、`/history`）
与写路径（`batch_writer`）并发到达，踩过两个坑，均已修并留护栏：

1. **`max_size` 不能等于 `min_size`。** 三值相等时池在并发下**不扩容**：
   本机实测 40 个并发 `acquire()`，池大小恒为设定值，超出的协程全部阻塞在
   `_queue.get()` —— 池退化成信号量。（反证：`min_size=2, max_size=10` 同场景涨到 10。）
2. **建池必须串行化。** `get_pool()` 的 `if _pool is None` 是 check-then-act，
   而 `create_pool()` 会 `await` 让出事件循环 —— 并发调用方会各建一个池。
   告警引擎实测过：ticker 首轮 100 台机器人同时进来 → 建出 **100 个池** →
   `TooManyConnectionsError` **332 次**。故加 `asyncio.Lock` + 二次检查。
   （此坑服务侧对称存在，`alert_engine/app/db.py` 同样修了。）

> 另注：`command_timeout` 只覆盖普通语句，**拦不住**被锁阻塞的批量写入（见上）。

## 关于 /history 的实现说明（ISSUE #76 修订）

`spec/00 §4.3` 的 `robot_status_10s` **不含 JSONB 关节列**，而 Drawer 的热力图与
第三条折线需要它们，故本端点此前一直直查 `robot_telemetry` 原始表并 `time_bucket`。

ISSUE #76 新增了专用连续聚合 **`robot_history_10s`**（`infra/init.sql §5.1`，
含 `joint_temperatures / joint_positions / joint_torques` 三列），`/history` 改为：
**先查聚合 → 结果为空则回退原始表**。

- 为什么 JSONB 能进连续聚合：`last(value, time)` 首参是 **anyelement 多态**
  （timescaledb `src/agg_bookend.c` 的 `ts_last_sfunc`），与 INTEGER 走同一路径；
  内部 partialize 亦为 `ANYELEMENT`（`sql/partialize_finalize.sql`）。
  原「缺列」是当初没写，不是能力不支持。
- 为什么不复用 `robot_status_10s`：它已被 R-08 §A4 / `test_integration.sh` /
  `tests/e2e` 三处断言，改列会动摇既有验收面；且两者消费方不同（看板最新态 vs 单机历史）。
- 为什么保留回退：`WITH NO DATA` 的聚合在首次刷新前是空的，不回退则冷启动期曲线恒空。
- 每条 SQL 列清单由 `tests/test_history_source.py` 逐字锁死（两分支不得漂移，
  且三个关节列必须同时在），防「一侧加列另一侧漏列」的间歇故障。
