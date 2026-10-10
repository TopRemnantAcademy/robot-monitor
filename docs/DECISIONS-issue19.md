# Issue #19 三项决策（2026-10-10）

> 用户原话：*「这些由你来替我决定，给我方案，方向」*。
> 本文是**决策记录 + 依据**，不是分析长文。每条决策都区分「事实」与「判断」，
> 可执行的已直接落进代码（见各节「落地」）。

---

## 决策总览

| # | 议题 | 决策 | 落地 |
|---|---|---|---|
| 1 | 遥测保留窗口 | **30 天** | `infra/init.sql` §7（已实现） |
| 2 | 告警通知渠道 | **自写通用 webhook 转发**（钉钉/企微/generic 三态），**不引 keep** | `alert_engine/app/notifiers/`（已实现） |
| 3 | 派单顺序 | **先 I-17，后 I-12**；两者可并行，I-17 是止血 | 本文 §3 |

---

## 决策 1：保留窗口 = 30 天

### 事实（容量估算，逐列算的，不是"感觉"）

行宽按 `infra/init.sql` 的 `robot_telemetry` 逐列累加：

| 部分 | 估算 |
|---|---|
| 标量列（time/robot_id/status/error/battery×2/max_joint/pos×3/orient×4） | ~110 B |
| 3 × JSONB（各存 24 关节浮点，文本化后约 300 B/个） | ~900 B |
| PG 行头 24 B + 页/索引开销 ×1.3 | — |
| **合计** | **≈ 1.34 KB/行** |

```
1000 row/s × 86400 s = 86,400,000 行/天
86,400,000 × 1.34 KB ≈ 116 GB/天（原始）
```

| 保留 | 原始 | 压缩 10x | 压缩 15x |
|---|---:|---:|---:|
| 30 天 | 3.5 TB | **≈ 350 GB** | ≈ 230 GB |
| 90 天 | 10.4 TB | ≈ 1.0 TB | ≈ 700 GB |

> 与上一轮口头的「30 天压缩后约 400 GB」**一致**（本轮补上了逐列依据）。

### 判断

选 **30 天**，理由三条：

1. **单机可容纳**。350 GB 落在常规工厂服务器 SSD/HDD 能力内，不需要为保留期单独特批存储。
2. **业务语义够用**。看板是**实时运维**（秒级~分钟级），历史诉求集中在
   「交班复盘（≤1 天）」与「周复盘（≤7 天）」。30 天已覆盖一周以上的追溯，
   且连续聚合 `robot_status_10s` 可另设更长保留做长期趋势（体量小两个数量级）。
3. **90 天的边际收益不划算**。多出的 60 天要多备 ~650 GB + 备份窗口，
   换来的只是"能翻三个月前某台机器人某一毫秒的关节扭矩" —— 工厂不会这么用。

### 落地（已在 `infra/init.sql` §7）

- `robot_telemetry`：压缩 `compress_after=1 day`、保留 `drop_after=30 days`
- `robot_alert`：保留 **180 天**（合规/复盘，且体量极小：即便每天 1 万条也只有百万级）
- 幂等用 `timescaledb_information.jobs` 存在性判断，**不用** `if_not_exists`
  （该参数本仓未验证，理由见 `init.sql` 内注释）

### ⚠️ 未验证

本机**无 Docker、无 PostgreSQL、无 timescaledb 扩展**，`init.sql` §7 的语句
**没有实际执行过**。已做的是：
- PL/pgSQL 结构静态校验（`DO $$`/`END $$;` 配对、`IF`/`END IF;` 配对、括号平衡）→ 全过
- `add_*_policy` 与 `ALTER TABLE ... SET (timescaledb.compress, ...)` 的**语法形式**
  与 `external/timescaledb` 库源码生成的形式逐字一致（`compression/sql.py:66-77`、
  `retention/sql.py:7-9`）
- 视图名与列名 `timescaledb_information.jobs` / `proc_name` / `hypertable_name`
  → 实证自 `external/timescaledb/retention/sql.py:47-58`

**仍需**：`docker compose up` 后确认 policy 注册成功（`SELECT * FROM timescaledb_information.jobs`）。

---

## 决策 2：告警通知渠道 = 自写通用 webhook，不用 keep

### 事实

1. `keep` **没有**钉钉/企微 provider —— 实证 `keep/keep/providers/` 里是
   `slack / teams / telegram / webhook / ntfy`（`grep -i ding|wecom` 无命中）。
2. 工厂侧机器人（钉钉自建机器人 / 企微群机器人）的接入方式都是
   **一个 HTTP POST + 固定 JSON**，不需要一个事件管理平台。
3. 引 `keep` = 多一个要运维、要备份、要升级的服务；而我们要的能力只有"转发一条消息"。
4. `grafana/oncall` **已归档**（`README.md:3`：2026-03-24 归档），不可选。

### 判断

**先按"通用 webhook = 三态适配"自写一个转发器，不引 keep。**

理由同时也是"接口预留"的考量 —— 判定逻辑写在这里，将来要换实现不用改调用方：

- 钉钉：`{"msgtype":"markdown","markdown":{"title":...,"text":...}}`
- 企微：`{"msgtype":"markdown","markdown":{"content":...}}`（**键名不同**，写错即 400）
- generic：原始字段直传，交工厂自建转发服务加工
- 门槛：默认 `min_severity=3`，只推 critical，**防低电量风暴刷屏**
- URL 含 token → **只从环境变量读**（`ALERT_WEBHOOK_URL`），不进仓库（AGENTS.md §5）
- 未配置 URL → 整条链路静默跳过（**默认关闭**，避免误配刷屏）

### 落地（已在 `alert_engine/`）

- 新增 `app/notifiers/webhook.py`（标准库 `urllib`，**零新增依赖**）
- 接入点：`app/main.py:53-66` 的 `emit()`，**在 `save()` 之后**（先落库保证审计，再通知）
- 配置：`app/config.py:44-51`（`alert_webhook_url/kind/min_severity`）
- 实证：`python -m tests.test_notifier` → **14/14 通过**

### ⚠️ 未验证

- **没有真实钉钉/企微机器人可测** —— 单测用假 `urlopen` 拦截，验证的是
  "payload 形状 + 业务码判定 + 异常不外抛"，**不是**真实投递。
- `asyncio.to_thread` 在 `emit()` 里是**串行 await**（每条告警等通知返回）。
  当前告警是低频事件（1000 msg/s 里零星几条），可接受；若将来告警密集，
  需改成有界队列 + 单消费者，避免 `emit` 被网络 RTT 拖慢。

---

## 决策 3：派单顺序 —— 先 I-17，后 I-12

### 事实（依赖分析）

| 单 | 触碰文件 | 是否互相阻塞 |
|---|---|---|
| I-17 数据生命周期 | `infra/init.sql` | — |
| I-12 观测 exporter | 新建 `infra/observability/*.yml`（+ 可能改 `infra/docker-compose.yml`） | 与 I-17 **无文件交集** |

`grep` 实证：`infra/init.sql` 此前**零** `retention`/`compression` 命中，
即 I-17 是纯新增，不动任何现有表结构语义。

### 判断

**① I-17 → ② I-12**，且两者**可并行**（无文件交集）。

- **I-17 先**：磁盘是**定时炸弹**（116 GB/天，一周写满），且是**最便宜的止损**
  （只动一个 SQL 文件，不引入任何运行时组件）。**止血类改动必须先做。**
- **I-12 紧随**：它是"度量"。但要注意 —— **度量是为后续改动服务的**，
  在磁盘即将写满时先上 exporter，只能让你更早看到磁盘满的曲线。
- **I-12 的真正约束**：exporter 必须与 `backend`/`redis`/`timescaledb` **同网络**，
  否则 `DATA_SOURCE_NAME` 解析不到（前几轮踩过的坑）。
- 之后顺序（沿用 §7.11）：I-15 契约 → I-13 认证 → I-14 通知（**本次已提前做掉**）→ I-16 压测。

> 注：**I-14 本轮已随决策 2 落地**（通知器 + 单测），不再排队等 I-13 的认证 ——
> 因为通知的"收件人"是群机器人 URL，与"谁登录了系统"无关，**不存在依赖**。
> 原计划里"I-14 依赖 I-13 的身份"这条**被本轮事实推翻**。

---

## 附：本轮诚实边界

- **无 Docker / 无 PostgreSQL** → `init.sql` §7 与通知器的**端到端**均未实跑。
  已跑通的只有：通知器单测 14/14、SQL 结构静态校验、`compileall`。
- 通知器**未接真实钉钉/企微**（无可用机器人）。
- 30 天 vs 90 天的**压缩率（10x）是行业典型值**，非本项目实测 ——
  需真实 TimescaleDB 灌数据后 `SELECT * FROM hypertable_compression_stats('robot_telemetry')` 确认。
