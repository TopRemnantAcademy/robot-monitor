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

### 对照结论（已据此修正本仓库）

| 项 | 官方写法 | 本仓库修正前 | 判定 |
|---|---|---|---|
| EMQX 镜像名 | `emqx/emqx-enterprise:5.0.3`（企业版）<br>开源版对应 `emqx/emqx` | `emqx:5.6`（**缺 org 前缀**） | ❌ **缺陷，已修** |

**实证过程（2026-10-10，digest 级验证）**

```
$ docker manifest inspect emqx:5.6
  digest: sha256:06f2341d1bf4ec8350a37a029dc5f6590c2dc95f181bebf1493d0f8e3a727f8e
          mediaType: application/vnd.oci.image.index.v1+json
$ docker manifest inspect emqx/emqx:5.6
  digest: sha256:c5a4118b2dc3bc0e8b0ed877eb7c51b7d9a4a5a920c20e441d5316fa43611ce0
          mediaType: application/vnd.docker.distribution.manifest.list.v2+json
$ curl https://hub.docker.com/v2/repositories/emqx/emqx/tags/5.6
  -> sha256:c5a4118b2dc3bc0e8b0ed877eb7c51b7d9a4a5a920c20e441d5316fa43611ce0  (与官方一致)
```

两个 digest **完全不同** → 裸名 `emqx:5.6` 解析到 Docker Hub 上的**同名非 EMQX 镜像**。
照修正前的 compose 启动会拉起一个根本不是 MQTT broker 的容器。
已修正为 `emqx/emqx:5.6`（`infra/docker-compose.yml`）。

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
git clone --depth 1 https://github.com/empicano/aiomqtt.git
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

## 5. 第四批：全量复扫补齐遗漏（2026-10-10 收口复扫）

> 触发：Issue #19「确保没有遗漏才行，继续找出可以组装的开源项目」。
> 复扫方法：**不看名字看依赖** —— 逐个扫 `requirements.txt` / `package.json` 里的
> 每一个包，以及项目「要真实跑起来还必须懂什么」，逐项回溯其上游源。
> 结论：**又发现 8 个遗漏**，其中 3 个是此前完全无参考源的关键项。

### ⭐ 遗漏 1：`timescale/timescaledb-docker`（官方部署契约）

| 项 | 值 |
|---|---|
| 来源 | `https://github.com/timescale/timescaledb-docker`（Timescale 官方组织） |
| stars / 体积 | 353 / 220 KB |
| 用途 | **TimescaleDB 官方镜像的权威部署契约**：镜像 tag 命名规则、`docker-entrypoint-initdb.d` 行为、`timescaledb-tune` 的 env 开关 |

**为什么此前漏了**：第一批只下了 `mqtt-to-timescaledb` 拿 EMQX 契约，
但**数据库侧自己的官方镜像契约反而没下** —— 这是对称性的遗漏。

**实证**：官方 `README.md:20,56,68` 展示的两个关键能力，我们**都没用上**：
- `ts_tune_*` 系列 env（`TS_TUNE_MEMORY` / `TS_TUNE_NUM_CPUS` / `TS_TUNE_MAX_CONNS`）—— 
  我们在 `infra/docker-compose.yml` 里**一个都没设**，容器按默认内存自调，压测时不可控。
- `NO_TS_TUNE=true` 开关 —— 明确关闭自动调优，测试环境需要关掉才能复现固定基线。

### ⭐ 遗漏 2：`timescale/timescaledb-tune`（参数调优工具本体）

| 项 | 值 |
|---|---|
| 来源 | `https://github.com/timescale/timescaledb-tune` |
| stars / 体积 | 507 / 632 KB |
| 用途 | 官方 PG/TSDB 参数调优器；镜像内已内置，源码提供 `shared_buffers` / `max_connections` / `work_mem` 的推导规则 |

**实证**（`timescaledb-docker/docker-entrypoint-initdb.d/001_timescaledb_tune.sh:1-8`）：
镜像的 entrypoint 就是调它。懂它才知道 `TS_TUNE_*` env 到底改了什么 —— 否则只是照抄。

### ⭐ 遗漏 3：`locustio/locust`（**broker 层压测的第二条路，且官方内置 MQTT**）

| 项 | 值 |
|---|---|
| 来源 | `https://github.com/locustio/locust` |
| stars / 体积 | 28199 / 18 MB |
| 用途 | Python 负载测试框架，**官方 contrib 内置 MQTT 客户端** |

**这是本轮最有价值的发现**。此前 broker 层压测只有 `emqtt-bench`（Erlang，
需 OTP 27.2+ 编译或下预编译二进制，上手门槛高）。而 locust：

```
locust/locust/contrib/mqtt.py        557 行，MqttUser / MqttClient
locust/examples/mqtt/locustfile.py   官方 MQTT 压测示例
```

**实证** `locust/locust/contrib/mqtt.py`：
- `:72` `class MqttClient(mqtt.Client)` —— 基于 **paho.mqtt**（我们第三批已下的源）
- `:516` `class MqttUser(User)`；`:521-522` `transport = "tcp"` / `ws_path = "/mqtt"`
- `:419 publish` / `:459 subscribe` —— 可压 pub 也可压 sub

**与已有两个工具的分工**（三者互补，非替代）：

| 工具 | 层 | 语言/门槛 | 输出 |
|---|---|---|---|
| `simulator/mock_fleet.py` | 端到端业务链路 | Python，已有 | 100 台真实 schema 负载 |
| `emqtt-bench` | broker 层裸上限 | Erlang，需 OTP | msg/s 上限 |
| **`locust` + `contrib.mqtt`** | broker 层 + **可写断言** | Python，pip 即装 | 带 SLA 判定的压测报告 |

**关键增益**：locust 能写**带断言的自定义压测脚本**（`--check` 失败率、
`events` 钩子），比 emqtt-bench 只能吐数字更贴合 R-07「可判定」的要求。

### 遗漏 4-8：补齐 set / 版本核对（此前无源）

| 需求点 | 仓库 | 体积 | stars | 套用结论 |
|---|---|---|---|---|
| **WS 服务端底层** | `python-websockets/websockets` | 4.0 MB | 5732 | 我们锁 `websockets==12.0`，上游 **17.3** —— 见 I-8 |
| **ASGI 服务器** | `Kludex/uvicorn` | 3.1 MB | 11007 | 锁定 `0.28.0`，上游 **0.54.0** —— 见 I-7 |
| **ORM 本体** | `sqlalchemy/sqlalchemy` | 34 MB | 12211 | 我们锁 `2.0.28`，上游 **2.1.5**；**仅读路径使用** —— 见 I-9 |
| **settings 管理** | `pydantic/pydantic-settings` | 1.8 MB | 1475 | 锁 `2.2.1`，上游 **2.15.0** —— 见 I-10 |
| **指标暴露范式** | `prometheus/node_exporter` | 4.5 MB | 13831 | 与 `client_python` 配套，提供 collector 写法范式（C3 用） |

### 第四批的复现命令（新增部分）

```bash
cd external
git clone --depth 1 https://github.com/timescale/timescaledb-docker.git
git clone --depth 1 https://github.com/timescale/timescaledb-tune.git
git clone --depth 1 https://github.com/locustio/locust.git
git clone --depth 1 https://github.com/python-websockets/websockets.git
git clone --depth 1 https://github.com/Kludex/uvicorn.git
git clone --depth 1 https://github.com/sqlalchemy/sqlalchemy.git
git clone --depth 1 https://github.com/pydantic/pydantic-settings.git
git clone --depth 1 https://github.com/prometheus/node_exporter.git
```

### 第四批中**不下**的（新增留档）

| 仓库 | 体积 | 理由 |
|---|---|---|
| `prometheus/prometheus` | **286 MB** | 我们只做指标暴露（pull 模型用 client_python 即可），不需要 server 源码 |
| `apache/superset` | **1235 MB** | 体量致命；BI 平台是自研大屏的**替代品**不是依赖 |
| `dataease/dataease` | **343 MB** | 同上，国产大屏成品 |
| `thingsboard/thingsboard` | **404 MB** | 完整 IoT 平台，与本项目「自研 2D 看板」定位冲突 |
| `home-assistant/core` | **893 MB** | 体量致命，领域不匹配（家居而非工业遥测） |
| `grafana/k6` | 83 MB | 已由 locust 覆盖 Python 压测需求，跨语言引入不划算 |
| `sqlfluff/sqlfluff` | 53 MB | `init.sql` 体量小，手工审查已足够 |
| `dromara/go-view` | 11 MB | Vue 技术栈，与本项目 React 不符 |
| `tremorlabs/tremor` | 4.7 MB | React 图表组件，但已由 ECharts 覆盖 |
| `bvaughn/react-window` | 8.9 MB | 告警表当前体量不需要虚拟滚动（属过度设计） |
| `lucide-icons/lucide` | 26 MB | 图标已由内联 SVG/现有方案覆盖 |
| `timescale/timescaledb` | 65 MB | 主体是 C 扩展；SQL 层契约已由 `timescaledb-docker` 覆盖 |

> **原则不变**：参考源必须是**官方组织仓库**。`grafana` / `superset` / `thingsboard`
> 这类「整平台」一律不下 —— 它们是**替代方案**，不是**依赖**，下了只会污染判断。

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

## 5. 第四批：平台「非功能面」缺口（2026-10-10 全量复扫 · CodeBuddy）

前三批复扫的都是**功能层**依赖（MQTT/PG/Redis/前端/告警）。
本批换个问法：**这套系统上线第一天就会缺什么？** —— 答案是四类，
前三批一个都没覆盖。

### 5.1 可观测性（最狠的缺口：整个仓库没有 /metrics）

| 需求点 | 仓库 | 体积 | stars | 许可 | 套用结论 |
|---|---|---|---|---|---|
| 指标 TSDB + 抓取 | `prometheus/prometheus` | 33 MB | 66459 | Apache-2.0 | 部署契约 → **已接线** |
| EMQX 补充指标 | `emqx/emqx-exporter` | 2.3 MB | 41 | Apache-2.0 | demo compose + 看板 → **已套用** |
| EMQX 内置端点实证 | `emqx/emqx`（sparse `apps/emqx_prometheus/`） | 444 KB | 16780 | Apache-2.0 | file:line 实证 → 见下 |
| HTTP/TCP 探针 | `prometheus/blackbox_exporter` | 736 KB | 5894 | Apache-2.0 | 模块配置 → **已套用** |
| PG/落库观测 | `prometheus-community/postgres_exporter` | 1.1 MB | 3630 | Apache-2.0 | DATA_SOURCE_NAME → **已接线** |
| Redis 观测 | `oliver006/redis_exporter` | 916 KB | 3696 | MIT | 端口/镜像 → **已接线** |
| 看板渲染 | `grafana/grafana` | **2011 MB** | 77222 | **AGPL-3.0** | ⚠️ 全量不下，见 5.4 |

> `emqx-exporter` 只有 41 stars，**不是**因为质量差 —— 它是 EMQX 官方组织仓库，
> 但主要面向企业版缺失指标的补齐。判断依据：它自带
> `grafana-dashboard/template/emqx-5/` 7 个看板 JSON + `examples/docker-compose/` 完整栈，
> 属「拿来就能接」的形态。stars 低 ≠ 不可用，这里用**官方组织归属 + 自带 demo 完整性**作为判据。

### ⭐ 关键发现：EMQX 的 Prometheus 端点是**内置**的，不需要 exporter

`external/emqx-prometheus-sparse/apps/emqx_prometheus/src/emqx_prometheus_api.erl:63`

```
        "/prometheus/stats" => ?SCOPE_MONITORING,
```

（同文件 `:60` 为 `/prometheus`，`:64-67` 为 `data_integration` / `schema_validation` /
`message_transformation` / `topic_metrics`）

→ `http://emqx:18083/api/v5/prometheus/stats` 直接可抓，Prometheus 无需任何 exporter
即可拿到 EMQX 的全部 Publish/Subscribe/连接/积压指标。**exporter 只补它没覆盖的**。

这条是**实证得到的**（读 fetched 源码），不是查文档抄的 —— 因为它直接决定了
`ops/prometheus/prometheus.yml` 里第一个 job 的 `metrics_path` 写什么。

### 5.2 性能诊断（不知道瓶颈在哪，就只能靠猜）

| 需求点 | 仓库 | 体积 | stars | 许可 | 套用结论 |
|---|---|---|---|---|---|
| Python 火焰图 | `benfred/py-spy`（sparse README） | 36 KB | 15557 | MIT | **`cap_add: [SYS_PTRACE]`** → 见下 |

**这个必须记下来，否则压测期一定踩**：
`external/py-spy/README.md:217-232`

> ### How do I run py-spy in Docker?
> This error is caused by docker restricting the `process_vm_readv` system call…
> This can be overridden by setting `--cap-add SYS_PTRACE` when starting the docker container.
>
> ```yaml
> your_service:
>    cap_add:
>      - SYS_PTRACE
> ```

→ 后端容器不加 `SYS_PTRACE`，`py-spy` 在容器内**必报 Permission denied**。
本仓库的 `backend_ingestion/Dockerfile` 目前没有这一行。（未接线，见文档 §6）

### 5.3 数据保护（最容易被忽略、且真会丢数据）

| 需求点 | 仓库 | 体积 | stars | 许可 | 套用结论 |
|---|---|---|---|---|---|
| 物理备份 + WAL 归档 | `pgbackrest/pgbackrest` | 19 MB | 4428 | 见仓库 LICENSE | 配置契约 → 已交付，**未接线** |
| 备份目标对象存储 | `minio/minio` | 40 MB | 61331 | **AGPL-3.0** | ⚠️ 备选，未采用 |

**为什么这是缺口**：`infra/init.sql` 的 `robot_telemetry` 是 `(time, robot_id)` 超表，
100 台 × 10Hz ≈ **8,640,000 行/天**。整个仓库**没有任何备份路径**，
`docker compose down -v` 一次历史归零，看板「历史折线」永不可复现。

> MinIO 的现状需要标注（实证 README 首行）：
> **「THIS REPOSITORY IS NO LONGER MAINTAINED.」**（推荐迁 AIStor）
> → 因此**不作为首选**，只登记为备选；且 AGPL-3.0 同样有传染性提醒。

### 5.4 关于 grafana 全量 clone：改用 sparse 参考

`grafana/grafana` 的 GitHub API `size` = **2,064,477 KB ≈ 2011 MB**，不可接受。
但它有价值的部分只有一个目录。本次采用**稀疏检出的下游仓库**：

`external/grafana-provisioning-ref/`（来源 `fkerem/AgLab`，★0，size 2885 KB）

理由（这是**判断**，注明依据）：它包含
- `docker-compose.yml:25-51` —— Grafana 12.3.2 的完整跑法
- `grafana/provisioning/datasources/instructor-postgres.yml` —— **`type`/`uid`/`jsonData.timescaledb` 的确切写法**

这两样正是「Grafana 自动接 TimescaleDB」的关键，而 Grafana 官方文档没给可执行的 YAML。
用 ★0 的仓库采证据有风险 → **因此在 `datasources.yaml` 里已把它标为「未验证」**，
且保守选了 Grafana 全系兼容的 `type: postgres` 而非参考里的 `grafana-postgresql-datasource`。

另：实测 `grafana/grafana` 内置数据源目录
（`public/app/plugins/datasource`，main 分支）只有
`alertmanager / azuremonitor / cloudwatch / dashboard / grafana-testdata-datasource /
grafana / graphite / mixed` —— **没有 timescaledb**。所以 TimescaleDB 只能走 postgres 数据源。

### 5.5 评过但**不下载**的（含体积实证，避免重复搜索）

| 仓库 | GitHub API size | clone 后实测 | stars | 不下的理由 |
|---|---|---|---|---|
| `Tencent/TAD_Sim` | 643,179 KB | **354 MB** | 468 | 想接真机仿真，但体积 + GPU + ROS 依赖，对本项目「2D 运维看板」收益为负 |
| `OpenBMB/ChatDev` | 381,817 KB | **301 MB** | 34477 | 与 AgentVerse 能力重叠，取轻的 |
| `debauchee/barrier` | 85,633 KB | **27 MB** | 30916 | 软 KVM，管键鼠不对管遥测，用不上 |
| `tsl0922/ttyd` | 20,567 KB | **2.7 MB** | 12486 | 浏览器终端，与 Grafana 运维窗口功能重叠 |
| `grafana/loki` | 563,797 KB | — | 29005 | 日志聚合；本项目日志量（结构化 stdout）远未到需要 Loki |
| `bloomberg/memray` | 33,165 KB | — | 15372 | 离线内存分析，与 py-spy 定位不同层，压测期先要 CPU 火焰图 |
| `git.macports/…` 等零散仓库 | — | — | — | 未列入 |

**唯一进 `external/` 的「大件」是 `AgentVerse`（31 MB）**，
理由（判断）：spec 里 06 阶段是「联调压测」，但**没有一个模块承担「多机器人任务级仿真」**。
AgentVerse 提供 task-solving + simulation 两套框架，是把它补上的现成起点。
**未验证**：它是否真能驱动 100 台并发 agent（其 README 明确是 LLM 多 agent 框架，不是物理仿真）。
