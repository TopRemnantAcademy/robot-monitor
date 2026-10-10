# 外部参考源（download first, don't hand-write）

本目录存放**实际下载**的第三方/官方仓库，用于对照实现、避免凭空手写。
目录本身已加入 `.gitignore`（不把第三方源码灌入主仓），但**本文件入库**，
以便 CNB NPC 与人工审阅者能看到参考来源及其对照结论。

下载方式一律为 `git clone --depth 1 <url>`，来源均为 GitHub 官方组织仓库。

---

## 1. `mqtt-to-timescaledb` — EMQX 官方

- 来源：`https://github.com/emqx/mqtt-to-timescaledb`（EMQX 官方组织 `emqx`）
- 下载：`git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git`
- 体积：1.1 MB ｜ 关键文件：`create-table.sql`、`docker-compose.yml`、`emqx/cluster.hocon`
- License：随仓库保留

### 对照结论

| 项 | 官方写法 | 本仓库 | 判定 |
|---|---|---|---|
| EMQX 镜像 | `emqx/emqx-enterprise:5.0.3`（企业版）<br>开源版为 `emqx/emqx` | `emqx/emqx:5.6` | ✅ 等价，保留 |

### 1.1 ⚠️ 撤稿：此前「`emqx:5.6` 是非 EMQX 镜像」的结论是**错的**

2026-10-10 曾按 digest 差异判定「裸名 `emqx:5.6` 解析到非 EMQX 镜像」。
**该结论已作废**，原因是**对比方法本身有误**，与镜像无关：

**错误 1 —— 拿「子 manifest 的 digest」当「镜像 id」，再跨仓库比**

```
library/emqx:5.6  index digest              = sha256:4cbb5792d0fd17e02f3003cda571aeca343d954f05215f8ab20a5a3b21603755
  └─ amd64 子 manifest digest               = sha256:06f2341d1bf4ec8350a37a029dc5f6590c2dc95f181bebf1493d0f8e3a727f8e
emqx/emqx:5.6     index digest              = sha256:b14a3c6d04f680869e3ca181b552c9338b7cd20c2d43d8af6c1e3b8aec48a0cc
```

文档里被写成「`emqx:5.6` → `06f2341d…`」的，其实是 `library/emqx` 的**子 manifest**digest，
却被拿去和 `emqx/emqx` 的 **index** digest 比。粒度不同，必然"对不上"。

**错误 2 —— 忽略了「同一镜像两个发布通道」这一事实**

`library/emqx` **就是**由 `emqx/emqx-docker` 构建的（Docker Official Images）：

```
$ curl https://raw.githubusercontent.com/docker-library/official-images/master/library/emqx
  Maintainers: Rory Z (@rory-z), Ivan Dyachkov (@id)
  GitRepo: https://github.com/emqx/emqx-docker.git     ← 与 emqx/emqx 同源
  Tags: 5.7.2, 5.7      Directory: 5.7
  Tags: 5.8.8, 5.8, 5, latest   Directory: 5.8
```

两个通道的 index digest 不同是**构造方式不同导致的必然结果**（bashbrew 重新构建 vs 官方 CI）。
EMQX 官方 README 也把 `emqx/emqx` 与 `emqx`(=library/emqx) 并列推荐。

**正面证据：`library/emqx:5.6` 确实是 EMQX 5.6.1**

```
$ curl -H "Authorization: Bearer $T"     https://registry-1.docker.io/v2/library/emqx/blobs/sha256:3a5ff702742d32ef92978cc8fe0122f1a127f136e7c006de0a807b0c7f7324b9
  config.Env        = ["...", "EMQX_VERSION=5.6.1", "AMD64_SHA256=a5be376…", ...]
  config.Entrypoint = ["/usr/bin/docker-entrypoint.sh"]
  config.Cmd        = ["/opt/emqx/bin/emqx", "foreground"]
  config.WorkingDir = "/opt/emqx"
  config.ExposedPorts = 1883 / 8883 / 18083 / 4370 / 5369 / 8083 / 8084
```

与 `emqx/emqx-docker` 5.6 分支 `Dockerfile`（`ENV EMQX_VERSION=5.6.1`、同 Entrypoint/Cmd、
同 EXPOSE 列表）**逐行吻合**。

**那为什么本仓库仍保留 `emqx/emqx:5.6`？**（此判断有依据，非因循守旧）

`library/emqx` 当前**只维护 5.7 / 5.8**（上方 official-images 文件的 Tags 段实证）。
`library/emqx:5.6` 是 Docker Hub 上遗留的**非受管 tag**，仍在但不会再获得
安全更新，也不会随 official-images 流程重新发布。
在 5.6 这一档上，`emqx/emqx:5.6` 才是持续发布的**可追溯来源**。

> 教训：digest 只能用来判断「字节是否相同」，**不能**用来判断「是不是同一个项目」。
> 跨发布通道比 digest 之前，先确认两者的**构建来源**是否同一。

**未采纳的官方写法（有意为之，非疏漏）**

**未采纳的官方写法（有意为之，非疏漏）**

- 官方 `create_hypertable('energy_log','timestamp','factory_id',4)` 第 4 参是
  **space partition 数**，面向「设备数为列」的宽表。本仓库是窄表
  （每台机器人每时刻一行），未使用 space partition。
- 官方示例走 **EMQX Rule Engine 直写 TimescaleDB**。该能力属 EMQX **企业版**，
  本仓库锁定开源版（`emqx/emqx`），故保留 FastAPI(aiomqtt) 中转路线 ——
  中转是必需的：Redis 状态缓存、告警判定、WS 推送都在这一层完成。
- 官方示例无连续聚合、无主键；本仓库两者都有（CAgg 供看板，PK 供去重）。
- 官方 compose 顶层声明了 `networks: emqx-net` 但无任何 service 引用 —— 属官方冗余，未模仿。

---

## 2. `tsbs` — TimescaleDB 官方压测套件

- 来源：`https://github.com/timescale/tsbs`（Timescale 官方组织 `timescale`，★1451）
- 下载：`git clone --depth 1 https://github.com/timescale/tsbs.git`
- 体积：3.0 MB
- 用途：**订单 06（集成与压测）** 的写入吞吐基线参考。
  `cmd/` 下提供 `tsbs_load_timescaledb` 等 loader，`pkg/targets/` 定义时序数据模型。
- 当前状态：已下载，**尚未使用**。订单 06 派单时应要求 NPC 先读
  `tsbs/README.md` 与 `docs/` 下的压测方法，再决定是复用其 loader
  还是沿用本仓库自有的 `simulator/`（后者更能精确产生 24 关节人形机器人负载）。

---

---

## 3. 第二批：按需求点全量下载（2026-10-10）

**套用与连接计划见 `external/INTEGRATION-PLAN.md`**（本文只记录来源与取舍）。

| 需求点 | 仓库 | 体积 | stars | 套用结论 |
|---|---|---|---|---|
| Broker 层压测 | `emqx/emqtt-bench` | 350 KB | 568 | 订单 06 接入，与 `mock_fleet.py` 互补（见 I-4） |
| MQTT 客户端契约 | `empicano/aiomqtt` | 430 KB | 577 | 契约核对 → I-1 升级、I-2 实例复用 |
| 前端状态管理 | `pmndrs/zustand` | 3.6 MB | 58801 | 版本核对，**决定不升级**（见 I-3 决策表） |
| 异步 Redis 用法 | `redis/redis-py` | 18 MB | 13647 | 版本核对，**决定不升级** |
| 全栈结构参考 | `fastapi/full-stack-fastapi-template` | 3.5 MB | 45929 | 结构对照 |
| 告警设计标准 | `prometheus/alertmanager`（sparse `docs/`） | 816 KB | 8642 | 对照发现缺「抑制」机制（I-5） |
| 指标暴露 | `prometheus/client_python` | 14 MB | 4380 | 订单 06 可选 |

> ⚠️ `aiomqtt` 仓库已从 `sbtinstruments/aiomqtt` **迁移至 `empicano/aiomqtt`**
> （实证：`gh api repos/sbtinstruments/aiomqtt` 返回 `empicano/aiomqtt`）。
> PyPI 包名 `aiomqtt` 未变，`requirements.txt` 无需改包名。

### 同上：搜索过但**不下**的（体积/价值比不划算或已被包管理器覆盖）

| 仓库 | 体积 | 理由 |
|---|---|---|
| `grafana/grafana` | **2011 MB** | 体量致命；本项目自研看板，Grafana 是替代方案不是依赖 |
| `apache/echarts` | 264 MB | 已由 npm 引入，实证 `frontend_dashboard/node_modules/echarts` 存在 |
| `pydantic/pydantic` | 458 MB | pip 已引入，458 MB 换类型注解细节不划算 |
| `timescale/timescaledb` | 65 MB | 主体是 C 扩展源码，本项目只需 SQL 层契约（已由官方示例覆盖） |
| `fastapi/fastapi` | 53 MB | pip 已引入，6 MB 的 template 已覆盖结构需求 |

> **原则**：已由包管理器（npm/pip）引入的依赖**不再 clone 源码**。
> clone 只用于拿「包里没有的东西」—— 部署契约、最佳实践文档、压测工具。

---

---

## 4. 第三批：补齐数据层与前端桥接的遗漏（2026-10-10 全量复扫）

复扫发现**核心数据层依赖此前完全没有参考源** —— `asyncpg` 是批量落库的命脉。

| 需求点 | 仓库 | 体积 | stars | 套用结论 |
|---|---|---|---|---|
| **异步 PG 驱动** | `MagicStack/asyncpg` | 2.0 MB | 8105 | **I-6 必升 0.29.0 → 0.30+**，见下 |
| 前端图表桥接 | `hustcc/echarts-for-react` | 292 KB | 5005 | 直接依赖核对 |
| WS 底层实现 | `Kludex/starlette` | 7.0 MB | 12654 | WebSocket 契约核对 |
| DB 迁移 | `sqlalchemy/alembic` | 4.1 MB | 4438 | 未来迁移方案评估 |
| MQTT 底层客户端 | `eclipse-paho/paho.mqtt.python` | 1.3 MB | 2429 | aiomqtt 的下层依赖 |

### ⭐ 关键发现：`asyncpg` 0.29.0 是「本机无法验证」的**根因**，不是环境限制

| 版本 | 发布日 | Python 支持 |
|---|---|---|
| `v0.29.0`（我们锁的） | 2023-11-05 | `requires-python = '>=3.8.0'`，**无 3.13 classifier** |
| `v0.30.0` | 2024-10-20 | **"Support Python 3.13 and PostgreSQL 17"** |
| `v0.31.0` | 2025-11-24 | + Python 3.14 |
| `v0.32.0`（最新） | 2026-10-06 | + Python 3.15 |

HEAD 版 `pyproject.toml` classifiers 实证列出 `3.9/3.10/3.11/3.12/3.13/3.14`。

此前把「`asyncpg==0.29.0` 在 Python 3.13 编译失败（`_PyLong_AsByteArray` 签名变更）」
归为**环境错误**，实为**版本过旧**。升 0.30+ 后该阻塞消失。

### ⭐ 附带发现：PyPI 403 可用镜像源绕过

本机 `files.pythonhosted.org` 返回 403，但**清华镜像源可用**：

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple asyncpg
# 实测：Python 3.13.14 + asyncpg 0.32.0 安装成功并可 import，耗时 29s（有 wheel）
```

这意味着此前标注「未验证（本机无法运行）」的后端项，**现在可以真实运行验证了**。

### 第三批中不下的

| 仓库 | 体积 | 理由 |
|---|---|---|
| `puppeteer/puppeteer` | **1533 MB** | 体量致命；已由 npm 引入（`frontend_dashboard/node_modules`） |
| `redis/redis` | 212 MB | 我们只用客户端；`redis-py` 已覆盖需求 |

---

## 复现命令

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
git clone --depth 1 https://github.com/MagicStack/asyncpg.git
git clone --depth 1 https://github.com/hustcc/echarts-for-react.git
git clone --depth 1 https://github.com/Kludex/starlette.git
git clone --depth 1 https://github.com/sqlalchemy/alembic.git
git clone --depth 1 https://github.com/eclipse-paho/paho.mqtt.python.git
```

> 另注：`encode/uvicorn` 仓库已迁至 **`Kludex/uvicorn`**（实证：API 返回 `Kludex/uvicorn`）。

---

## 搜索过但**判定不值得下载**的仓库（留档，避免重复搜索）

| 仓库 | stars | 不下载的理由 |
|---|---|---|
| `zmops/zeus-iot` | 729 | 完整 IoT 平台，体量与依赖远超本项目需求 |
| `KouShenhai/KCloud-Platform-IoT` | 674 | 同上，Java 技术栈，与本项目 Python/TS 不符 |
| `dave-wind/awesome-python` | — | 仅单个 `app.py` 示例，无 schema/compose 参考价值 |
| `innovaassolutions/MQTTtoMCP` | — | 耦合 OpenAI/GPT 查询层，与本项目无关 |
| `robot fleet monitoring dashboard` 类（`Panca2022/Robot-Monitor-Dashboard` 等） | 0–2 | 玩具项目，stars 过低，代码质量无保障 |

> 原则：宁可少下载，也不把低质量代码当作「参考」引入。
> 参考源必须是**官方组织仓库**或经 digest/stars 实证的高质量仓库。

---

## 5. 第四批：全量执行下载 + 版本漂移实证（2026-10-10 复扫）

按 §1–§4 清单**全量执行** `git clone --depth 1`，14/14 全部成功。
体积与 stars 预期一致，无一个失败。实测清单（`git rev-parse --short HEAD`）：

| 仓库 | 体积 | HEAD | 远程 |
|---|---|---|---|
| `emqx/mqtt-to-timescaledb` | 1.2 MB | `ee24974` | emqx/mqtt-to-timescaledb |
| `timescale/tsbs` | 2.9 MB | `8323e59` | timescale/tsbs |
| `emqx/emqtt-bench` | 388 KB | `cb86b18` | emqx/emqtt-bench |
| `empicano/aiomqtt` | 872 KB | `c8f8248` (v2.5.1) | empicano/aiomqtt |
| `pmndrs/zustand` | 3.6 MB | `d7a5583` | pmndrs/zustand |
| `redis/redis-py` | 18 MB | `22d47b5` | redis/redis-py |
| `fastapi/full-stack-fastapi-template` | 3.6 MB | `7257e60` | fastapi/full-stack-fastapi-template |
| `prometheus/alertmanager`（sparse docs） | 892 KB | `e7a4afd` | prometheus/alertmanager |
| `prometheus/client_python` | 14 MB | `9cd073c` | prometheus/client_python |
| `MagicStack/asyncpg` | 2.0 MB | `13b0191` | MagicStack/asyncpg |
| `hustcc/echarts-for-react` | 372 KB | `2346edc` | hustcc/echarts-for-react |
| `Kludex/starlette` | 7.1 MB | `96d5023` | Kludex/starlette |
| `sqlalchemy/alembic` | 4.1 MB | `1bc21b8` | sqlalchemy/alembic |
| `eclipse-paho/paho.mqtt.python` | 1.4 MB | `b48baee` | eclipse-paho/paho.mqtt.python |

### ⚠️ 实证发现：`aiomqtt` 的 `--depth 1` 已漂移到 v3.0.0-alpha.1

| 项 | 事实 | 证据 |
|---|---|---|
| 裸 `--depth 1` 拉到的 HEAD | **`3.0.0-alpha.1`**（v3 重写线） | `pyproject.toml` `version = "3.0.0-alpha.1"` |
| v3 的破坏性变更 | 弃用 paho-mqtt 改 `mqtt5`；`Client` 参数全 keyword-only；`MqttError` 被 `ConnectError` 等替代；`reconnect=True` 成为内建能力 | `CHANGELOG.md` `## [3.0.0-alpha.1]` 段 |
| 本项目锁定版本 | **`2.5.1`**（`backend_ingestion/requirements.txt`） | `==` 锁死 |
| `INTEGRATION-PLAN.md` 的 I-1/I-2 证据 | 引 `docs/reconnection.md`（v2.5.1 路径） | 裸 clone **找不到该文件** |

**处置**：已将 `external/aiomqtt` 显式 checkout 到 `v2.5.1` 标签
（`git fetch --depth 1 origin refs/tags/v2.5.1:refs/tags/v2.5.1 && git checkout v2.5.1`，
HEAD 变为 `c8f8248`）。切回后 `docs/reconnection.md` 与
`docs/alongside-fastapi-and-co.md` **均存在**，I-2 引用的
「reusable (but not reentrant)」原文落在 `docs/reconnection.md:5` —— 与计划一致。

> 教训：`--depth 1` 只钉住"当下 HEAD"，不钉住版本。参考源若被用于核对某个
> **锁定版本**的 API，必须 `checkout <tag>`，否则下次复扫会静默拿到新主线的
> 破坏性变更，导致 file:line 证据失效。

### 其余仓库的关键文件抽验（全部命中）

| 仓库 | 抽验文件 | 结果 |
|---|---|---|
| `mqtt-to-timescaledb` | `create-table.sql`、`docker-compose.yml`、`emqx/cluster.hocon` | ✅ 存在 |
| `tsbs` | `README.md`、`cmd/`、`docs/` | ✅ 存在 |
| `emqtt-bench` | `README.md` 含 `--prometheus` | ✅ `README.md:101` |
| `alertmanager` | `docs/configuration.md` 含 `inhibit_rules` | ✅ `:165-167` |
| `asyncpg` | `pyproject.toml` classifiers 含 3.13/3.14 | ✅ `:26-27` |
| `starlette` | `starlette/websockets.py` | ✅ 存在 |
| `paho.mqtt.python` | `src/paho/mqtt/client.py` | ✅ 存在 |
| `echarts-for-react` / `zustand` / `redis-py` / `alembic` / `full-stack-fastapi-template` | `package.json` / `redis/asyncio/client.py` / `setup.cfg` / `README.md` | ✅ 存在 |

## 6. 第五批：从零重扫（含「完全替换」候选）—— 2026-10-10

> **本轮方法变了**。前四批是「看名字猜需求」，本轮按 **32 项审计**里
> 每一条**尚未有参考源的需求点**逐条回溯上游，并且**主动找更完美的替代品**
> （不是补缺，是问「现在选的这个是不是最好的」）。
>
> 判定口径：①需求点是否真的存在（有 `file:line` 或审计编号支撑）
> ②仓库是否活跃（`pushed_at`）③是否有官方发布镜像/可直接跑（决定「下载」还是「装服务」）
> ④是否有更优替代（可替换现有依赖）。

### 6.1 本轮新增 20 个源（全部 `git clone --depth 1`，实测可达）

| # | 需求点（对应审计/缺口） | 仓库 | 体积 | HEAD | 关键实证 |
|---|---|---|---|---|---|
| N1 | MQTT 缺口→OTel 采集管线（#32 数据新鲜度 / 统一遥测） | `open-telemetry/opentelemetry-python` | 12 MB | `c4d8ff6` | `opentelemetry-sdk/` 存在 |
| N2 | 前端 RUM/追踪（审计「前端无观测」） | `open-telemetry/opentelemetry-js` | 22 MB | `e5db961` | `api/`、`e2e-tests/` |
| N3 | 采集器/边缘转发（统一 metrics+logs+trace） | `open-telemetry/opentelemetry-collector` | 22 MB | `e85aabc` | 官方 collector |
| N4 | 采集器发行版（配置化 pipeline，含 Docker） | `grafana/alloy` | 69 MB | `eabe529` | `collector/README.md`、`docs/` |
| N5 | **PG 指标**（护栏「无 metrics 出口」的直接解） | `prometheus-community/postgres_exporter` | 1.4 MB | `fb6be9d` | `README.md:252` `DATA_SOURCE_NAME` |
| N6 | **Redis 指标**（Redis 是单点，必须可观测） | `oliver006/redis_exporter` | 1.2 MB | `6de1e8b` | `README.md:197` `REDIS_ADDR`、`:216` `/metrics` |
| N7 | **可写断言的压测**（R-07/R-08「可判定」） | `grafana/k6` | 75 MB | `b408672` | `go.mod` `go.k6.io/k6/v2` |
| N8 | k6 的 MQTT 扩展（补上「MQTT 能压」） | `pmalhaire/xk6-mqtt` | 312 KB | — | 312 KB，`git ls-remote` 17 refs |
| N9 | **认证/RBAC**（审计「grep auth 零命中」） | `fastapi-users/fastapi-users` | 1.2 MB | `e9c57ff` | `__init__.py` `15.0.5`；`pyproject.toml:133` `fastapi>=0.65.2` |
| N10 | 企业级 SSO（预留，非默认） | `keycloak/keycloak` | 205 MB | `eb6d2dde` | Java，官方镜像 |
| N11 | 轻量 SSO（单厂更划算） | `casdoor/casdoor` | 38 MB | `43a6aab` | Go+Vue |
| N12 | **事件/告警闭环**（ack + 升级 + 值班） | `keephq/keep` | 133 MB | — | `providers/` 含 `slack/teams/telegram/webhook/ntfy` |
| N13 | Alertmanager 本体（抑制/静默真实现） | `prometheus/alertmanager` | 7.1 MB | `e7a4afd` | `docs/configuration.md` `inhibit_rules` |
| N14 | 告警 → 企业 IM 桥（Python，可裁剪） | `idealista/prom2teams` | 1.0 MB | `aaecfd5` | Teams webhook 转发 |
| N15 | 状态页/公告（运维对外） | `openstatusHQ/openstatus` | 218 MB | — | `docker-compose.yaml` |
| N16 | **Pydantic 契约生成**（防 24 关节人肉比对） | `koxudaxi/datamodel-code-generator` | 99 MB | `7ae9017` | `README.md:327` `--url ... --output model.py` |
| N17 | **TS 契约生成**（同一 OpenAPI → `types/robot.ts`） | `openapi-ts/openapi-typescript` | 90 MB | `d46a319` | 生成 `types`/`openapi-fetch` |
| N18 | 单测框架（审计 #18「数字不可重复」） | `pytest-dev/pytest` | 12 MB | `4fa645f` | 官方 |
| N19 | 前端测试（E2E 真跑大屏） | `microsoft/playwright` | 108 MB | `d9f2fd3` | 官方 |
| N20 | 组件测试 + Mock（审计 #23/#31） | `vitest-dev/vitest`、`testing-library/react-testing-library`、`mswjs/msw` | 70/1.4/6.9 MB | — | 官方 |

### 6.2 数据生命周期与灾备（补齐审计 #7/#28）

| # | 需求点 | 仓库 | 体积 | 关键实证 |
|---|---|---|---|---|
| N21 | 保留/压缩/连续聚合（每天 ~130 GB 定时炸弹） | `timescale/timescaledb` | 64 MB | `add_retention_policy` / `add_compression_policy` 主体 |
| N22 | 高级时序函数（gapfill 等，看板可用） | `timescale/timescaledb-toolkit` | 3.9 MB | 扩展 SQL 函数 |
| N23 | PG 备份 | `pgbackrest/pgbackrest` | 22 MB | 官方 |
| N24 | WAL 归档备份（云对象存储） | `wal-g/wal-g` | 35 MB | 官方 |

### 6.3 替代评估（**「即使完全替换也要找到更完美的」**）

| 现有选择 | 候选替代 | 结论 | 依据 |
|---|---|---|---|
| `emqtt-bench`（Erlang，只吐数字、难写断言） | **`k6` + `xk6-mqtt`** | ✅ **建议替换/并列采用** | k6 是 Go 自带 JS 断言 + 官方发布；`emqtt-bench` 需 OTP 27.2+ 编译。R-07 要「可判定」，k6 直接满足 |
| `mock_fleet.py` 纯自研 | `k6` 亦可生成负载 | ➖ **保留自研** | 24 关节真实 payload 只有自研能精确产生（I-4 判断不变） |
| `grafana/oncall`（前轮已推荐） | — | ❌ **撤回** | **实证：`README.md:3` 原文 "entered maintenance mode on 2025-03-11 and was archived on 2026-03-24"** |
| 无值班/升级机制 | `keephq/keep` | ✅ 新引入 | 活的、多 provider、docker-compose 可直跑 |
| `zustand`（已决定不升） | `tanstack/query` | ➖ **补充而非替换** | query 管服务端状态/缓存/重试，与 zustand 管客户端状态互补，不互相替代 |
| 无错误边界（审计 #23） | `react-error-boundary` | ✅ 小成本直用 | 2.4 MB，官方推荐姿势 |
| `alert_engine` 自研规则引擎 | `prometheus/prometheus` 规则 | ❌ **不替换** | 规则引擎要判「机器人」，Prometheus 判「平台」。**同名不同层**（前轮已结论） |
| 自研 `deduplicator.py` | Alertmanager `inhibit_rules` | ➖ 学语义不换实现 | 已按 I-5 抄语义；引 Alertmanager 实例反而多一层运维 |

### 6.4 本轮**发现的陷阱**（实证，不是推测）

1. **`victoriametrics/victoria-metrics` 是 404**（`curl -sI` → `HTTP/2 404`）。
   前轮 `INTEGRATION-PLAN.md` 已注明「ls-remote 失败已划掉」，**本轮复核确认死链**，非临时网络问题。
   正确仓库是 `VictoriaMetrics/VictoriaMetrics`（1777 refs）—— 但我们**不引入**（判：与 Prometheus 功能重叠，见 §5.5）。
2. **Grafana OnCall OSS 已归档**（见 §5.3）。
3. `git ls-remote` 遇大仓偶发 `refs=0`（如 `grafana/loki` 首次），**必须复核**，
   否则会把活仓误判为死仓 —— 这是「可达性核对」本身的坑。

### 6.5 搜索过但**明确不下载**（本轮新增，附理由）

| 仓库 | 体积 | 不下的理由 |
|---|---|---|
| `thingsboard/thingsboard` | 404 MB | **整平台（替代方案）**，非依赖；引入即推翻现有四层架构 |
| `openremote/openremote` | 106 MB | 同上；已下载仅作**架构对照**（多租户/fleet 模型），不套用 |
| `apache/bifromq` | 17 MB | 分布式 MQTT broker，**替代 EMQX**；100 台规模用不上分布式 |
| `nanomq/nanomq` | 33 MB | 边缘 broker，**替代 EMQX**；EMQX 生态（Dashboard/Rule）更全 |
| `illacloud/illa-builder` | 52 MB | 低代码看板，**替代自研大屏**；交付物就是自研 2D 看板，不用 |
| `grafana/grafana` | 2064 MB | 前轮已判不下，本轮**维持** |
| `grafana/loki` + `vectordotdev/vector` + `fluent/fluent-bit` | 442+73+243 MB | 日志栈。**价值真实**（审计 #27/#19 要「有日志可查」），但 700+ MB 换一份「怎么配」的文档不划算 —— **改为记录推荐配置**，需要时按官方镜像起服务 |
| `openstatusHQ/openstatus` | 218 MB | 状态页是**对外**产品需求，本项目是对内大屏，属「增值」非「必需」—— 已下载留档，列为 P3 |
| `chakra-ui/ark` | 134 MB | 无头 UI 原语，**替代手写组件**；当前 Tailwind 手写已过 tsc，收益 < 回归风险 |
| `goauthentik/authentik` | 157 MB | SSO，与 Keycloak/Casdoor 三者取一已够，选更轻的 |

> ⚠️ **本轮自纠**：初扫时下了 `ark-ui`/`fluent-bit`/`loki`/`authentik` 共 ~1 GB，
> 复核后**按本仓既有原则（不把体量致命/替代方案引入）删除**，只留归因结论。
> 已下载 ≠ 该下载，这条对 NPC 同样适用。

---

## 复现命令（第五批增量）

```bash
cd external
# 可观测性
git clone --depth 1 https://github.com/grafana/alloy.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-collector.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-python.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-js.git
git clone --depth 1 https://github.com/prometheus-community/postgres_exporter.git
git clone --depth 1 https://github.com/oliver006/redis_exporter.git
git clone --depth 1 https://github.com/grafana/k6.git
git clone --depth 1 https://github.com/pmalhaire/xk6-mqtt.git
# 认证 / 告警闭环
git clone --depth 1 https://github.com/fastapi-users/fastapi-users.git
git clone --depth 1 https://github.com/keycloak/keycloak.git
git clone --depth 1 https://github.com/casdoor/casdoor.git
git clone --depth 1 https://github.com/keephq/keep.git
git clone --depth 1 https://github.com/prometheus/alertmanager.git
git clone --depth 1 https://github.com/idealista/prom2teams.git
# 契约即代码
git clone --depth 1 https://github.com/koxudaxi/datamodel-code-generator.git
git clone --depth 1 https://github.com/openapi-ts/openapi-typescript.git
# 测试与质量
git clone --depth 1 https://github.com/pytest-dev/pytest.git
git clone --depth 1 https://github.com/microsoft/playwright.git
git clone --depth 1 https://github.com/vitest-dev/vitest.git
git clone --depth 1 https://github.com/testing-library/react-testing-library.git
git clone --depth 1 https://github.com/mswjs/msw.git
# 数据生命周期 / 灾备
git clone --depth 1 https://github.com/timescale/timescaledb.git
git clone --depth 1 https://github.com/timescale/timescaledb-toolkit.git
git clone --depth 1 https://github.com/pgbackrest/pgbackrest.git
git clone --depth 1 https://github.com/wal-g/wal-g.git
```

### 本轮验证边界（诚实标注）

| 结论 | 状态 |
|---|---|
| 20+ 仓库 `git clone` 成功（实测体积/HEAD 见 §5.1） | ✅ 实测 |
| `DATA_SOURCE_NAME` / `REDIS_ADDR` / `/metrics` 行号 | ✅ 实测（`grep -n`） |
| `fastapi-users 15.0.5` 与 `fastapi>=0.65.2` 兼容区间 | ✅ 实测（源码） |
| Grafana OnCall 已归档 | ✅ 实测（`README.md:3` 原文） |
| `victoriametrics/victoria-metrics` 404 | ✅ 实测（`curl -sI`） |
| **未跑通任何新源的服务** | ❌ 无 Docker 环境，未起 k6/keep/OTel |
| **未实装 fastapi-users** | ❌ 未与 `fastapi==0.110.0` 做运行时联调 |
| `xk6-mqtt` 是否兼容 k6 v2 线 | ❌ 未编译（需 Go） |
