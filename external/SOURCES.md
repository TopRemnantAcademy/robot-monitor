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

## 复现命令

```bash
mkdir -p external && cd external
git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git
git clone --depth 1 https://github.com/timescale/tsbs.git
```

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
