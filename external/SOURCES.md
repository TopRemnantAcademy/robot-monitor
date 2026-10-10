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
