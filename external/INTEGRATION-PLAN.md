# 下载源 → 本项目的套用与连接计划

> 本文回答两个问题：**每个下载的东西具体套用到本项目的哪一行**，以及**按什么顺序连接**。
> 所有套用点均标注 `文件:行号`，可逐条核对。未经实证的标「未验证」。

---

## 0. 需求点 → 源映射（为什么下这些，不下那些）

| # | 本项目需求点 | 采用的顶级开源源 | 体积 | 套用方式 |
|---|---|---|---|---|
| 1 | MQTT Broker 部署契约 | `emqx/mqtt-to-timescaledb` | 1.1 MB | **已套用**（修正镜像名） |
| 2 | MQTT 客户端 API 契约 | `empicano/aiomqtt` | 430 KB | 契约核对，见 I-1/I-2 |
| 3 | Broker 层吞吐压测 | `emqx/emqtt-bench` | 350 KB | 订单 06 接入，见 I-4 |
| 4 | 时序库写入基线 | `timescale/tsbs` | 3.0 MB | 方法论参考 |
| 5 | 告警去重/抑制设计 | `prometheus/alertmanager`（sparse docs） | 816 KB | 设计对照，见 I-5 |
| 6 | 指标暴露 | `prometheus/client_python` | 14 MB | 订单 06 可选 |
| 7 | 异步 Redis 用法 | `redis/redis-py` | 18 MB | 版本核对 |
| 8 | 前端状态管理 | `pmndrs/zustand` | 3.6 MB | 版本核对 |
| 9 | 全栈工程结构 | `fastapi/full-stack-fastapi-template` | 3.5 MB | 结构对照 |
| 10 | **异步 PG 驱动（批量落库命脉）** | `MagicStack/asyncpg` | 2.0 MB | 版本核对 → **I-6 必升** |
| 11 | 前端图表桥接 | `hustcc/echarts-for-react` | 292 KB | 直接依赖核对 |
| 12 | WS 底层实现 | `Kludex/starlette` | 7.0 MB | WebSocket 契约核对 |
| 13 | DB 迁移 | `sqlalchemy/alembic` | 4.1 MB | 未来迁移方案评估 |
| 14 | MQTT 底层客户端 | `eclipse-paho/paho.mqtt.python` | 1.3 MB | aiomqtt 的下层依赖 |

**明确不下**（附理由，避免重复搜索）：

| 仓库 | 体积 | 不下的理由 |
|---|---|---|
| `grafana/grafana` | **2011 MB** | 体量致命；本项目自研看板，Grafana 是替代方案而非依赖 |
| `apache/echarts` | 264 MB | 已通过 npm 引入，**实证** `frontend_dashboard/node_modules/echarts` 存在；源码对本项目无增量价值 |
| `pydantic/pydantic` | 458 MB | pip 依赖已引入；458 MB 换取类型注解细节不划算 |
| `timescale/timescaledb` | 65 MB | 主体是 C 扩展源码；本项目需要的是 SQL 层契约，已由官方示例覆盖 |
| `fastapi/fastapi` | 53 MB | pip 依赖已引入；用 6 MB 的 template 覆盖结构需求已足够 |

> 原则：**已通过包管理器引入的依赖，不再 clone 源码**。clone 只用于获取
> 「包里没有的东西」—— 部署契约、最佳实践文档、压测工具。

---

## 1. 已完成的套用（本次实证后落地）

### I-0 ⚠️ EMQX 镜像（原「修正」已撤稿，2026-10-10 复核）

- 套用点：`infra/docker-compose.yml`（镜像保持 `emqx/emqx:5.6`，**改动只在注释与 healthcheck**）
- 源：`external/mqtt-to-timescaledb/docker-compose.yml`
- **原结论作废**：此前写「`emqx:5.6` 是非 EMQX 镜像」是**对比方法错误**造成的误判 ——
  拿 `library/emqx` 的**子 manifest** digest 去比 `emqx/emqx` 的 **index** digest。
  两者本就是同一镜像（`library/emqx` 由 `emqx/emqx-docker` 构建）的两个发布通道。
- **保留 `emqx/emqx:5.6` 的真实理由**：`library/emqx` 当前只维护 5.7/5.8，
  5.6 档上 `emqx/emqx:5.6` 才是持续发布的可追溯来源。
- 本次实际改动：healthcheck 补 `start_period: 60s`（EMQX 首次启动慢，R-07 §1 强调过假失败风险）。
- 完整实证与撤稿说明见 `external/SOURCES.md` §1.1

---

## 2. 待执行的套用项（按优先级）

### I-6 🔴🔴 升级 `asyncpg` 0.29.0 → 0.30+（**解锁本机验证，最高优先**）

- **套用点**：`backend_ingestion/requirements.txt`（当前 `asyncpg==0.29.0`）
- **源**：`external/asyncpg` + 官方 release notes
- **实证**：

  | 版本 | 发布日 | Python 支持 |
  |---|---|---|
  | `v0.29.0`（**我们锁的**） | 2023-11-05 | `requires-python = '>=3.8.0'`，**无 3.13 classifier** |
  | `v0.30.0` | 2024-10-20 | **"Support Python 3.13 and PostgreSQL 17"** |
  | `v0.31.0` | 2025-11-24 | + Python 3.14（实验性 subinterpreter / freethreading） |
  | `v0.32.0`（最新） | 2026-10-06 | + Python 3.15 |

  HEAD 版 `pyproject.toml` classifiers 实证列出 `3.9 / 3.10 / 3.11 / 3.12 / 3.13 / 3.14`。

- **为什么这是最高优先级**：
  此前记录的环境阻塞 —— *「`asyncpg==0.29.0` 在 Python 3.13 上编译失败
  （`_PyLong_AsByteArray` 签名变更）」* —— **根因不是环境，是我们锁的版本太旧**。
  升到 0.30+ 后 Python 3.13 是官方支持目标，该阻塞**消失**。
  这意味着后端可以在本机真实跑起来，而不是永远停留在「未验证」。
- **连带影响**：部署镜像锁 `python:3.11-slim`（有预编译 wheel），生产侧不受影响；
  升级是为了**开发/验证侧**可运行，顺带对齐 PostgreSQL 17。
- **风险**：低。0.29 → 0.32 为 minor 升级，`connect()` / `fetch()` / `executemany()` API 稳定。
  `COPY` 相关 API 若有使用需在 Docker 内回归一次。

### I-1 🔴 升级 `aiomqtt` 2.0.1 → 2.5.1

- **套用点**：`backend_ingestion/requirements.txt`
- **源**：`external/aiomqtt` + 官方 CHANGELOG（`empicano/aiomqtt@v2.5.1`）
- **实证**：当前锁 `aiomqtt==2.0.1`；最新稳定 `v2.5.1`（2026-03-05）
- **为什么必须升**（不是追新，是修 bug）：
  ```
  ## [2.5.0]
  - Let exceptions bubble up in `Client.__aexit__` instead of suppressing them
  ```
  我们的重连**完全依赖** `async with` 退出时抛 `MqttError` 来触发退避重连
  （`backend_ingestion/app/services/mqtt_subscriber.py:27-38`）。
  在 2.0.1 下 `__aexit__` 会**吞掉异常** → 重连分支可能根本不执行 → 连接断了却静默不重连。
  这是会直接导致"看板永远停在最后一帧"的隐患。
- **附带收益**：2.3.2 起测试 Python 3.13；2.3.1 起用 `get_running_loop` 替代已废弃的 `get_event_loop`
- **风险**：低（2.x 线内 minor 升级，`Client(hostname=..., port=...)` 签名在 2.5.1 已验证一致）

### I-2 🔴 复用 `aiomqtt.Client` 实例（官方明确要求）

- **套用点**：`backend_ingestion/app/services/mqtt_subscriber.py:31-33`
- **源**：`external/aiomqtt/docs/reconnection.md`（v2.5.1）
- **官方原文**：
  > The `Client` context is designed to be **reusable (but not reentrant)**.
  >
  > ```python
  > client = aiomqtt.Client("test.mosquitto.org")   # ← 循环外创建
  > while True:
  >     try:
  >         async with client:                       # ← 循环内复用
  >             ...
  > ```
- **我们当前**（每轮新建实例）：
  ```python
  while True:
      try:
          async with aiomqtt.Client(hostname=..., port=...) as client:   # ← 每轮 new
  ```
- **差异影响**：每轮新建 → 每轮生成新 `identifier` → broker 侧视为全新会话。
  功能上"能跑"，但违背官方设计，且重连时无法保留会话状态。
- **保留我们更好的部分**：官方示例用固定 `interval=5`，我们用指数退避 1→30s。
  在 1000 msg/s 场景下退避更合理，**不要退回固定 5s**。

### I-3 🟡 `lifespan` 关闭顺序修正

- **套用点**：`backend_ingestion/app/main.py:36-52`
- **源**：`external/aiomqtt/docs/alongside-fastapi-and-co.md`（v2.5.1）
- **官方顺序**：`cancel listener task` → `await task`（捕获 `CancelledError`）→ 退出 `async with`
- **我们当前**：
  ```python
  for t in (mqtt_task, writer_task):
      t.cancel()                        # ① 两个一起 cancel
  await writer.graceful_shutdown()      # ② 再冲刷 writer
  await asyncio.gather(mqtt_task, writer_task, return_exceptions=True)  # ③
  ```
- **问题**：`writer_task` 在 ① 已被 cancel，② 才去冲刷它的队列 —— 存在竞态，
  进程退出时可能丢失最后一批遥测（正是验收要看的 30,000 行附近）。
- **建议顺序**：先 cancel `mqtt_task`（停止接收新消息）→ `await writer.graceful_shutdown()`（冲刷存量）
  → 再 cancel `writer_task` → 最后 gather。
- **状态**：**未验证**（需运行时才能证明丢批，本机 PyPI 被墙无法跑）

### I-4 🟢 订单 06 接入 `emqtt-bench`（broker 层压测）

- **套用点**：`orders/06-dispatch.md` 的 T3（性能报告）
- **源**：`external/emqtt-bench`（EMQX 官方，Erlang 编写，★568）
- **它与 `simulator/mock_fleet.py` 是互补而非替代**：

  | 工具 | 测什么 | payload |
  |---|---|---|
  | `emqtt-bench pub` | **Broker 层上限**（EMQX 能扛多少） | 固定 `-s <size>` 字节 |
  | `mock_fleet.py` | **端到端链路**（MQTT→校验→Redis→落库→WS） | 符合 `Telemetry` schema 的 24 关节真实负载 |

  两者都跑才能区分「瓶颈在 broker」还是「瓶颈在我们的后端」—— 这正是订单 06 要的结论。
- **用法**（实证自 `external/emqtt-bench/README.md`）：
  ```sh
  emqtt_bench pub -h localhost -p 1883 -c 100 -t 'robots/%c/telemetry' -s 1024 -I 100 --prometheus
  ```
  自带 `--prometheus` 指标，可与 `client_python` 对齐
- **前置**：需 Erlang/OTP 27.2+ 编译，或直接用 releases 里的预编译二进制

### I-5 🟢 告警引擎对照 Alertmanager 设计

- **套用点**：`alert_engine/app/deduplicator.py`
- **源**：`external/alertmanager/docs/configuration.md`（sparse 取 docs，避开 57 MB 全量）
- **要对照的三个概念**（Alertmanager 是告警领域事实标准）：
  1. `group_by` — 同类告警聚合（我们按 `robot_id + rule_id` 去重，概念一致 ✓）
  2. `repeat_interval` — 去重窗口内不重复告警（我们的 `dedup_window_s`）
  3. **`inhibition`（抑制）** — 高 severity 告警出现时抑制低 severity
     → **我们目前没有这个机制**：机器人已判 `offline`(severity 3) 时，
     仍可能同时报 `battery_low`(severity 2)，造成同一台机器刷两条告警
- **建议**：先只记录为「后续建议」，不在订单 06 实施（避免污染收口验收）

---

## 3. 版本核对结论（**决定不升级**，附理由）

| 依赖 | 本项目 | 上游最新 | 决策 | 理由 |
|---|---|---|---|---|
| `zustand` | 4.5.5 | v5.0.15 | **不升** | v5 有 breaking change（默认导出移除等）。当前 4.5.5 已实证 `tsc` 0 错误 + `vite build` 通过，升级收益 < 回归风险 |
| `redis[hiredis]` | 5.0.3 | v8.1.0 | **不升** | 跨 3 个主版本；我们只用 `redis.asyncio` 基础 API 与 pubsub。锁版本可复现 |
| `fastapi` | 0.110.0 | 0.143.0 | **不升** | 0.x 线 API 稳定；用到的 `lifespan`/`WebSocket`/`HTTPException` 无变化 |
| `echarts` | 5.5.1 | — | **不升** | 已装且构建通过 |

**唯一例外是 I-1 的 `aiomqtt`** —— 因为它有直接影响重连正确性的 bug 修复，
不是"追新"，是"修错"。这是本计划的差异化判断点。

---

## 4. 连接顺序（谁先谁后）

```
① I-1 升级 aiomqtt ──┐
② I-2 Client 复用    ─┼─→ 同属 mqtt_subscriber.py，一起改、一起验
                      │
③ I-3 关闭顺序 ───────┘  同属 backend，随 ①② 一并处理

④ I-4 emqtt-bench ─────→ 订单 06（CNB @codebuddy 正在跑）
⑤ I-5 告警抑制 ────────→ 订单 06 之后，作为 v1.1 建议
```

- ①②③ 依赖同一个文件，必须**一起改**避免半套状态
- ④ 已写入 `orders/06-dispatch.md` 的必读清单，NPC 可直接消费
- ⑤ 明确后置，不进订单 06

---

## 5. 复现下载命令

```bash
mkdir -p external && cd external
git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git
git clone --depth 1 https://github.com/timescale/tsbs.git
git clone --depth 1 https://github.com/emqx/emqtt-bench.git
git clone --depth 1 --branch v2.5.1 https://github.com/empicano/aiomqtt.git
git clone --depth 1 https://github.com/pmndrs/zustand.git
git clone --depth 1 https://github.com/redis/redis-py.git
git clone --depth 1 https://github.com/fastapi/full-stack-fastapi-template.git
git clone --depth 1 https://github.com/prometheus/client_python.git
git clone --depth 1 --filter=blob:none --sparse https://github.com/prometheus/alertmanager.git
cd alertmanager && git sparse-checkout set docs && cd ..
```

> 注意：`aiomqtt` 仓库已从 `sbtinstruments/aiomqtt` **迁移**至 `empicano/aiomqtt`
> （实证：`gh api repos/sbtinstruments/aiomqtt` 重定向）。PyPI 包名 `aiomqtt` 未变。

---

## 6. 验证边界（诚实标注）

| 结论 | 状态 |
|---|---|
| EMQX 镜像名 digest 差异 | ✅ 实测（`docker manifest inspect`） |
| aiomqtt `Client(hostname=,port=)` 签名 | ✅ 实测（v2.5.1 源码） |
| aiomqtt 2.5.0 `__aexit__` 修复 | ✅ 实测（CHANGELOG） |
| 官方 `Client` reusable 表述 | ✅ 实测（reconnection.md） |
| asyncpg 0.30+ 支持 Python 3.13 | ✅ 实测（release notes + `pyproject.toml` classifiers） |
| 各仓库 stars/size | ✅ 实测（GitHub API） |

### ⭐ 2026-10-10 追加实测：`external/aiomqtt` 必须钉 v2.5.1 标签

裸 `git clone --depth 1 https://github.com/empicano/aiomqtt.git` 当前拉到的是
**`3.0.0-alpha.1`**（v3 重写线，弃用 paho-mqtt、`MqttError` 被替换、参数全 keyword-only），
`docs/reconnection.md` 在该 HEAD 上**已不存在** —— 会导致本文 I-1/I-2 的 file:line 证据失效。

已处置：`external/aiomqtt` 显式 `git checkout v2.5.1`（HEAD `c8f8248`），
切回后 `docs/reconnection.md:5` 的「reusable (but not reentrant)」原文与本计划一致。
复现命令已改为 `--branch v2.5.1`。详见 `external/SOURCES.md` §5。

### ⭐ 2026-10-10 追加实测：本机验证通道已打通

此前两项标为「未验证」的阻塞**已解除**：

1. **PyPI 403 可用镜像源绕过** —— `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple`
2. **asyncpg 在 Python 3.13 装不上是版本问题** —— 换 0.32.0 后 29 秒装完

实测结果（Python **3.13.14** + 隔离 venv）：

```
aiomqtt==2.5.1   asyncpg==0.32.0   fastapi==0.143.0   redis==8.1.0   pydantic==2.14.0

[1] schemas.telemetry OK, joints = 24
[2] app.main import OK
[3] aiomqtt 2.5.1
[4] Client params: ['self', 'hostname', 'port', 'username']
```

**结论**：
- I-1（升 aiomqtt 2.5.1）**已验证安全** —— 我们的 `Client(hostname=..., port=...)`
  在 2.5.1 下签名一致，`app.main` 正常导入
- 后端模块在**最新依赖组合**下可导入，不存在隐藏的导入期不兼容

| 剩余未验证 | 原因 |
|---|---|
| I-2 / I-3 的**运行时**行为 | 需真实 MQTT broker + TimescaleDB（Docker），导入期已验证 |
| 端到端 30,000 行 | 同上，属订单 06 范围（CNB 执行中） |
