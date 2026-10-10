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
