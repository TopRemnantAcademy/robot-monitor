# R-09 数据库对接规格（TimescaleDB 衔接的唯一事实来源）

> 起因：Issue #19「数据库如何对接完全粗糙」。本文把「粗糙」拆成可验收的条目，
> 每条都给出**本机实测**结果与复现方式。未验证项单列，不混入通过项。

## 0. 验证环境（本轮实测，非推测）

| 项 | 值 |
| :--- | :--- |
| PostgreSQL | 15.19（Debian 15.19-0+deb12u1） |
| TimescaleDB | **2.13.1**（与 `infra/docker-compose.yml` 锁的版本一致） |
| Python | 3.11.2 |
| asyncpg | 0.29.0 |
| 复现命令 | `TEST_DATABASE_URL=... python -m tests.test_db_integration` |

> 本环境**无 Docker**，故用本机 PostgreSQL 15 + TimescaleDB 2.13.1 扩展替代
> `timescale/timescaledb:2.13.1-pg16` 容器。两者 PG 主版本不同（15 vs 16），
> 但本项目用到的 API（`create_hypertable` / 连续聚合 / `add_retention_policy` /
> `time_bucket` / `last()`）在 2.13.1 上行为一致。

## 1. 写入路径（核心）

### 1.1 选型依据不是吞吐

| 路径 | 5000 行耗时 | 吞吐 |
| :--- | :--- | :--- |
| `copy_records_to_table` | 5~32 ms | 45k~72k 行/s |
| `executemany(INSERT)` | 71 ms | 70.8k 行/s |

峰值 = 100 台 × 10Hz = 1000 行/s。两者都有 **45x~72x 冗余**。
→ 选型依据是**容错**，不是速度。见 §1.2。

### 1.2 三个实测硬事实

| # | 事实 | 实测证据 |
| :-- | :--- | :--- |
| F1 | COPY **不支持 `ON CONFLICT`** | 重复批次 → `UniqueViolationError: duplicate key` |
| F2 | COPY 传 `dict` 到 jsonb 列 → `TypeError: expected str, got dict` | 必须传 `json.dumps()` 文本 |
| F3 | **单行坏数据连坐整批**（COPY 与 executemany 均如此） | 10 好行 + 1 坏行 → 表内 **0** 行 |

### 1.3 三级降级链（必须实现）

| 级 | 路径 | 实测结果 |
| :-- | :--- | :--- |
| L1 | COPY → 临时表 → `INSERT ... SELECT ... ON CONFLICT DO NOTHING` | 200 行写入 + **重复批次行数不变**（幂等 ✅） |
| L2 | `executemany` | L1 全败后接管，200 行全部落库 ✅ |
| L3 | 二分隔离坏行 | 100 行含 1 坏行 → **99 行保住**，坏行计入 `rejected=1` ✅ |

> L3 的必要性：一批 500 条时，不隔离坏行 = 每 0.5 秒丢 500 条遥测。

### 1.4 幂等的正确写法（F1 的补丁）

```sql
CREATE TEMP TABLE telemetry_stg (LIKE robot_telemetry INCLUDING DEFAULTS);
COPY telemetry_stg (...) FROM STDIN;          -- asyncpg copy_records_to_table
INSERT INTO robot_telemetry (...) SELECT ... FROM telemetry_stg
  ON CONFLICT (time, robot_id) DO NOTHING;    -- 幂等在这里实现
```

实测：临时表中转后 jsonb 列 `jsonb_typeof` 仍为 `object`（未被降级成 text）。

## 2. 类型契约

| 项 | 要求 | 实测 |
| :--- | :--- | :--- |
| 关节列三个 | **JSONB**（不是 JSON） | `jsonb_typeof = object` ✅ |
| ORM 与 init.sql | 列类型必须一致 | 修复前 ORM 编出 `JSON`，与库内 `jsonb` **漂移** ❌→✅ |
| 关节 JSONB 可查询 | `->>` 可用（连续聚合/热力图前提） | `->>'left_shoulder_pitch' = 42.5` ✅ |
| `time` | 必须 `TIMESTAMPTZ`，时区感知 | COPY 传字符串会 `TypeError`，必须传 `datetime` ✅ |

## 3. 连续聚合与保留策略

| 项 | 实测 |
| :--- | :--- |
| `robot_status_10s` 创建 | `CREATE MATERIALIZED VIEW`（hypertable 2）✅ |
| 刷新策略 | `policy_refresh_continuous_aggregate`，start 1h / end 1m ✅ |
| 手动刷新 30,000 行 | 产出 **3,100** 个 10s 桶 ✅ |
| 保留策略 `robot_telemetry` | `drop_after = 30 days` 注册成功 ✅ |
| 保留策略 `robot_alert` | `drop_after = 90 days` 注册成功 ✅ |
| `last()` 在连续聚合可用 | 样例行返回正确 `status_code` / `battery_pct` ✅ |

## 4. `/history` 端点

实测（真实 TimescaleDB，灌入 5 分钟数据）：

- 返回 **31** 个点（5 分钟 / 10s，期望约 30）✅
- 字段齐全：`time / status_code / battery_pct / max_joint_temp / joint_*` ✅
- `joint_positions` **非空**（Drawer 第三条折线）✅
- 类型正确：`battery_pct: float`、`status_code: int`（非字符串）✅
- `resolution=500ms` → HTTP 400 ✅

## 5. 连接与可观测

| 项 | 要求 | 理由 |
| :--- | :--- | :--- |
| `statement_cache_size=0` | 关闭 | 兼容 PgBouncer(transaction 模式) |
| `max_inactive_connection_lifetime` | 300s | 回收前 ping，避免死连接复用 |
| `command_timeout` | 30s | 防止查询挂死占住连接 |
| `/healthz` 带 `db` 字段 | 必须 | 把「DB 不可达」暴露成可观测状态，而非等落库报错 |
| `/healthz` 探活缓存 | 5s | 就绪探针秒级调用，不能每次打库 |

### 5.1 `/healthz` 语义（刻意设计，勿改）

- `status: ok` / `degraded` 由 DB 可达性决定，**仍返回 HTTP 200**。
- 理由：这是**存活探针**语义。改成 503 会让容器被编排重启掉，而依赖恢复后本服务
  能自愈；重启反而会丢在飞数据。依赖不健康由 `status` 字段表达。
- 新增计数：`retries`（持续增长 = DB 不稳）、`rejected`（非 0 = 有脏数据）。

### 5.2 告警写入必须 `acquire()`

`pool.execute()` 在连接处于异常状态时可能把坏连接放回池中复用，导致连环失败。
告警是低基数事件（去重后 60~120s 一条/机器人），多一层 `acquire()` 无性能代价。

## 6. 未验证（诚实标注，不得当作已通过）

| 项 | 为什么没验证 | 需要什么 |
| :--- | :--- | :--- |
| `timescaledb` 压缩策略 | 压缩率与查询计划影响需实测，故在 `init.sql` 中**注释保留** | TimescaleDB 环境跑 `EXPLAIN` 对比 + 实测压缩率 |
| PG16 容器（`2.13.1-pg16`） | 本环境无 Docker | `docker compose up` 后重跑 `test_db_integration.py` |
| 端到端 30,000 行（含 MQTT/模拟器） | 需 EMQX + Redis + 容器；ISSUE #60 已备好容器化 harness（`tests/e2e/`），但本开发环境无 docker daemon，该 harness 尚未在真实 Docker 上产出实测数字 | `python -m pytest tests/e2e/test_e2e_30k.py -v -s`（ISSUE #60），或 `bash test_integration.sh`（订单 06） |
| 保留策略**实际删除**行为 | 要等 30 天或被删 chunk 存在才能观察 | 手动 `INSERT` 31 天前数据 → 跑 `run_job` 验证 |

## 7. 回归命令

```bash
# 单测（需真实 PostgreSQL + 已执行 init.sql）
cd backend_ingestion
TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:55432/rm_ts" python -m tests.test_db_integration
# 期望：13 通过 / 0 失败
```
