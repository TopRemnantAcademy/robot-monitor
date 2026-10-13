# 上游开源清单（Upstream Manifest）

> 目的：本仓库的**每一处外部能力**都能追到「哪个仓库、哪个版本、哪个文件、哪种许可」。
> 配套文档：`external/SOURCES.md`（下载了什么、为什么下）、`external/INTEGRATION-PLAN.md`（套用到哪一行）。
> 目录本身 gitignore（第三方源码不入主仓），但**这三份归因文档入库**。

## 1. 运行时依赖（包管理器引入，不 clone 源码）

| 组件 | 版本 | 许可 | 用途 |
|---|---|---|---|
| fastapi | 0.110.0 | MIT | 后端 HTTP/WS |
| uvicorn | 0.28.0 | BSD-3 | ASGI server |
| pydantic / pydantic-settings | 2.6.4 / 2.2.1 | MIT | 数据契约校验 |
| asyncpg | 0.29.0 → **0.32.0**（订单 08） | Apache-2.0 | PG 异步驱动 |
| sqlalchemy | 2.0.28 | MIT | ORM |
| redis[hiredis] | 5.0.3 | MIT | 状态缓存 / Pub-Sub |
| aiomqtt | 2.5.1 | BSD-3 | MQTT 客户端 |
| websockets | 12.0 | BSD-3 | WS |
| react / react-dom | 18.3.1 | MIT | 前端 |
| echarts | 5.5.1 | Apache-2.0 | 折线图 |
| echarts-for-react | 3.0.2 | MIT | React 桥接 |
| zustand | 4.5.5 | MIT | 前端状态 |
| tailwindcss | 3.4.17 | MIT | 样式 |

> `aiomqtt` 仓库已从 `sbtinstruments/aiomqtt` 迁到 `empicano/aiomqtt`（PyPI 包名不变）。

## 2. 运维依赖（容器镜像，docker-compose `ops` profile）

| 镜像 | 版本 | 许可 | 引入理由 |
|---|---|---|---|
| `emqx/emqx` | 5.6 | Apache-2.0（NOASSERTION，以仓库 LICENSE 为准） | MQTT broker |
| `timescale/timescaledb` | 2.13.1-pg16 | Apache-2.0 + PostgreSQL | 时序库 |
| `redis` | 7.2-alpine | RSALv2/SSPLv1（**非 OSI**） | 缓存；仅作独立容器调用，不受传染 |
| `prom/prometheus` | v3.1.0 | Apache-2.0 | 指标 TSDB |
| `emqx/emqx-exporter` | 0.2.3 | Apache-2.0 | EMQX 补充指标 |
| `prom/blackbox-exporter` | v0.26.0 | Apache-2.0 | HTTP/TCP 探针 |
| `prometheuscommunity/postgres-exporter` | v0.17.0 | Apache-2.0 | PG/时序写入观测 |
| `oliver006/redis_exporter` | v1.67.0 | MIT | Redis 观测 |
| `grafana/grafana` | 11.5.0 | **AGPL-3.0** ⚠️ | 运维看板 |

### ⚠️ AGPL 传染性提醒

`grafana/grafana` 与 `grafana/loki`、`minio/minio` 都是 **AGPL-3.0**。本仓库是 MIT。
「调用未修改的官方镜像」不触发 AGPL 的源码开放义务；
但**若将来修改 Grafana 源码并以网络服务形式对外提供**，就必须公开修改后的源码。
本条只是风险登记，不是当前阻塞项。

## 3. 只下载源码、不进运行时的参考源（`external/`，gitignore）

| 仓库 | HEAD | 体积 | 用途 |
|---|---|---|---|
| emqx/mqtt-to-timescaledb | `ee24974` | 1.2 MB | EMQX 部署契约（已修正镜像名） |
| timescale/tsbs | `8323e59` | 2.9 MB | 压测方法论 |
| emqx/emqtt-bench | `cb86b18` | 388 KB | broker 层压测 |
| empicano/aiomqtt | `c8f8248` (v2.5.1) | 872 KB | 客户端契约 |
| pmndrs/zustand | `d7a5583` | 3.6 MB | 版本核对 |
| redis/redis-py | `22d47b5` | 18 MB | 版本核对 |
| fastapi/full-stack-fastapi-template | `7257e60` | 3.6 MB | 工程结构 |
| prometheus/alertmanager | `e7a4afd` | 892 KB | 告警抑制设计 |
| prometheus/client_python | `9cd073c` | 14 MB | 指标暴露（未接入） |
| MagicStack/asyncpg | `13b0191` | 2.0 MB | 版本核对 |
| hustcc/echarts-for-react | `2346edc` | 372 KB | 前端桥接核对 |
| Kludex/starlette | `96d5023` | 7.1 MB | WS 契约 |
| sqlalchemy/alembic | `1bc21b8` | 4.1 MB | 迁移方案 |
| eclipse-paho/paho.mqtt.python | `b48baee` | 1.4 MB | 底层客户端 |
| **emqx/emqx**（sparse `apps/emqx_prometheus/`） | `7537a354` | 444 KB | **Prometheus 端点实证** |
| **emqx/emqx-exporter** | `dc299c94` | 2.3 MB | **运维层 job/看板模板** |
| **prometheus/prometheus** | `2213c3c1` | 33 MB | **scrape 配置与 promtool 契约** |
| **prometheus/blackbox_exporter** | `ea6d6cd6` | 736 KB | **探针模块** |
| **prometheus-community/postgres_exporter** | `fb6be9d8` | 1.1 MB | **PG 观测口径** |
| **oliver006/redis_exporter** | `6de1e8b0` | 916 KB | **Redis 观测** |
| **benfred/py-spy**（sparse README） | `b870defa` | 36 KB | **压测期火焰图（需 SYS_PTRACE）** |
| **pgbackrest/pgbackrest** | `ad72e581` | 19 MB | **备份配置契约（未接线）** |
| **minio/minio** | `7a2a2a2c` | 40 MB | 备份目标物化存储（备选） |
| **OpenBMB/AgentVerse** | `f90c4bd9` | 31 MB | 多机器人任务求解框架（LLM，**G-7 已评估未采纳**） |

> ⚠️ **AgentVerse 判定（Issue #114 / G-7）**：实测为 LLM 文本对话框架，`BaseAgent` 强制 `llm: BaseLLM`
> （`agentverse/agents/base.py:21`），`step()` 返回自然语言 `Message`，**不产出** `mock_fleet` 的遥测数值。
> 无 `OPENAI_API_KEY` 时连 `import agentverse` 都会因 `DEFAULT_CLIENT` 未定义而崩。
> **不引入**：其依赖钉 `pydantic==1.10.7` / `openai==1.1.0` / `fastapi==0.95.1`，与本仓 pydantic v2 栈冲突；
> 且 10Hz × 100 台的 LLM 往返延迟数量级不符。任务级仿真由 `simulator/task_sim.py` 承担。
> 完整实证（原始报错 + JSON 报告）见 `docs/agentverse-verification/README.md`。 |

## 4. 明确**不**纳入

| 仓库 | 体积 | 理由 |
|---|---|---|
| grafana/grafana（全量） | 2011 MB | 只取 provisioning 目录作参考，已由 `grafana-provisioning-ref` 覆盖 |
| apache/echarts | 264 MB | 已由 npm 引入 |
| pydantic/pydantic | 458 MB | 已由 pip 引入 |
| timescale/timescaledb | 65 MB | 主体是 C 扩展，SQL 层契约已由官方示例覆盖 |
| fastapi/fastapi | 53 MB | 已由 pip 引入 |
| puppeteer | 1533 MB | 已由 npm 引入 |
| redis（C 源码） | 212 MB | 已由镜像覆盖 |
| Tencent/TAD_Sim | 643 MB / 354 MB clone | **想接 TAD Sim 做真机仿真，但体积+GPU+ROS 依赖不划算；实测 clone 后 354 MB** |
| OpenBMB/ChatDev | 301 MB | 与 AgentVerse 能力重叠，取轻的那个 |
| debauchee/barrier | 27 MB | 软 KVM，只对「真机操作」有价值，遥测监控用不上 |
| tsl0922/ttyd | 2.7 MB | 终端进浏览器；本仓已用 Grafana 做运维窗口，冗余 |
