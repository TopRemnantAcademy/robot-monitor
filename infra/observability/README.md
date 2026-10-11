# 可观测性接线说明

> 回答一个问题：**这些开源下下来之后，到底接在哪、怎么跑。**
> 来源与取舍理由见 `external/SOURCES.md` §5；套用点 file:line 见 `external/INTEGRATION-PLAN.md` §6。

## 1. 一条命令起来

```bash
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.app.yml \
  -f infra/observability/docker-compose.observability.yml \
  up -d
```

三层各管什么：

| 文件 | 内容 | 为什么单独一层 |
|---|---|---|
| `infra/docker-compose.yml` | EMQX + TimescaleDB + Redis | 「跑通看板」的最小集，必须保持轻量 |
| `infra/docker-compose.app.yml` | backend + alert-engine | 应用要进同一网络，否则 Prometheus 的目标名解析不到 |
| `infra/observability/…` | prometheus + alertmanager + 2 个 exporter | 运维增强层，按需叠加 |

## 1.1 为什么 exporter 必须与 base 组件同网（订单 22 / I-12）

四个 exporter/prometheus 的目标地址全是 **Docker service DNS**（`timescaledb:5432`、
`redis:6379`、`backend:8000` …）。这些名字只有在**同一张网络**里才解析得到。

本仓库的做法是**三层都不声明 `networks:`**，让 Compose 把三个文件的所有 service
一起投进默认网络 `{project}_default`。`verify_wiring.sh` 的第 5 步会守住这条：
一旦有人在 observability 文件里加了 `networks:`，检查立即变红。

踩过的坑（这正是 I-12 反复强调的原因）：
exporter 容器会**正常起来**、端口也正常监听，但 `/metrics` 里没有任何业务指标 ——
因为 `DATA_SOURCE_NAME` 里的 `timescaledb` 解析失败。只看 `docker ps` 完全发现不了。

拼命令时 project 名必须一致：用**一条** `docker compose -f a -f b up`，或给每条命令
都加同一个 `-p <project>`。否则会造出两张网络，DNS 互不可见。

## 2. 端口与入口

| 服务 | 地址 | 用途 |
|---|---|---|
| backend | `:8000` | `/healthz`、`/robots`、`/ws/robots`、**`/metrics`**（均免鉴权；`/robots` 等业务 REST 需 Bearer） |
| alert-engine | `:8001` | **`/metrics`**（独立 HTTP 服务，不是 FastAPI） |
| Prometheus | `:9090` | UI / PromQL / `/-/rules` |
| Alertmanager | `:9093` | UI / 静默 / 抑制 |
| postgres-exporter | `:9187` | TimescaleDB 指标 |
| redis-exporter | `:9121` | Redis 指标 |

## 3. 抓取拓扑

```
prometheus.yml
├── prometheus                 localhost:9090      自身健康
├── robot_backend              backend:8000/m      backend_ingestion/app/metrics.py
├── robot_alert_engine         alert-engine:8001/m alert_engine/app/metrics.py
├── emqx                       emqx:18083/api/v5/prometheus/stats
├── postgres                   postgres-exporter:9187/m
└── redis                      redis-exporter:9121/m
```

## 4. 告警规则分两层，别混淆

| 层 | 是谁 | 判什么 | 落在哪 |
|---|---|---|---|
| 应用层 | `alert_engine` | **机器人**：过热 / 低电量 / 离线 / 关节卡死 | `robot_alert` 表 + Redis `robot:alerts` |
| 平台层 | Prometheus + Alertmanager | **监控系统自己**：broker down / 落库积压 / 抓取失败 | `rules/robot-alerts.yml` |

`robot_alert` 回答「哪台机器人坏了」；Prometheus 回答「监控系统还有没有资格回答前一个问题」。
后者失灵时，前者的所有结论都不可信。

### 4.0 命名唯一来源（ISSUE #72）

后端的指标前缀**只有 `robot_backend_*`**。历史上订单 16 的实现分支另立过一套
`robot_monitor_*`（6 个），与订单 19 已落地的 `robot_backend_*` 双轨并存 —— 其中 3 个
是同一计数源的重复指标（同一 `queue.qsize()` / `writer.written` / `mqtt.received`）。
现已统一：

| 旧名（已废） | 现状 |
|---|---|
| `robot_monitor_batch_queue_size` | 删除（同源 `robot_backend_queue_depth`） |
| `robot_monitor_batch_rows_total` | 删除（同源 `robot_backend_telemetry_written_total`） |
| `robot_monitor_mqtt_messages_total` | 删除（同源 `robot_backend_mqtt_received_total`） |
| `robot_monitor_batch_flush_total{reason}` | 并入 `robot_backend_batch_flushes_total{reason}` |
| `robot_monitor_ws_connections` | 改名 `robot_backend_ws_connections` |
| `robot_monitor_history_latency_seconds` | 改名 `robot_backend_history_latency_seconds` |

**面板/告警若引用过 `robot_monitor_*`，需同步改名**（本仓 rules 与 verify_wiring 已同步；
外部 Grafana 面板不在仓库内，属运维侧动作）。改名的两个指标 PromQL 写法不变，只是名字换了。

### 4.1 两个「WS 指标」不是一回事（易混）

| 指标 | 视角 | 谁能回答 | 谁在写 |
|---|---|---|---|
| `robot_backend_ws_connections` | **服务端** | 有几个客户端挂着 | `main.py` 的 `ws_robots` 连接/断开时增删 |
| `robot_backend_ws_probe_alive` | **客户端** | 这条连接真的还能收到首帧 snapshot 吗 | `app/ws_probe.py` 的合成探针循环 |
| `robot_backend_ws_probe_failures_total` | **客户端** | 断过几次（`increase()` 看抖动） | 同上 |

判据：**连接数 ≥ 1 且探针为 0** ⇒ 看板「假的绿」—— 网关卡了升级 / snapshot 抛异常 /
Redis Pub/Sub 断了，但连接对象还在。这正是缺口 G-3 要抓的事故。
探针免鉴权（自签 token 打 loopback）、≥30s 一次、可降级（`WS_PROBE_ENABLED=false` 即 no-op）。

## 5. 快速自检

```bash
# 1) 三个 exporter 的 /metrics 都有内容
curl -sf localhost:8000/metrics | grep robot_backend_queue_depth
curl -sf localhost:8001/metrics | grep robot_alert_robots_online
curl -sf localhost:9187/metrics | head

# 2) Prometheus 目标全 UP（不要只看容器在跑）
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c

# 3) 规则已加载
curl -s localhost:9090/api/v1/rules | grep -o '"name":"[A-Za-z]*"' | head
```

判据：`health` 全部 `up`。出现 `down` 时按下面排查。

## 6. 常见故障

| 现象 | 根因 | 处置 |
|---|---|---|
| `robot_alert_engine` 一直 down | 告警引擎进程没起 metrics 线程 | 确认 `alert_engine/app/main.py` 的 `main()` 调了 `metrics.start()` |
| `robot_backend` 一直 down | 应用跑在宿主机，`backend` 域名解析不到 | 用 `docker-compose.app.yml` 让应用进网络 |
| `emqx` down | `/api/v5/prometheus/stats` 需 API key | 该端点未验证；不要伪报 up=1，改用 emqtt-bench `--prometheus` |
| `queue_depth` 抓取后不变 | Gauge 只在写入时 set | 已有修正：`/metrics` 处理函数抓取前刷新（`main.py:86`） |
| postgres/redis-exporter 起来了但无业务指标 | exporter 与 timescaledb/redis **不同网**，`DATA_SOURCE_NAME` 的 host 解析失败 | 确认三个 compose 都没声明 `networks:`、且用同一条命令（或同一 `-p`）拉起 |
| 启动即报 `POSTGRES_PASSWORD 未设置` | `${POSTGRES_PASSWORD:?…}` 按设计拦住了无凭据启动 | 在宿主机 `cp .env.example .env` 并填写（见 `.env.example` 的 TimescaleDB 凭据段） |
| `robot_backend_ws_probe_alive` 恒为 0 | 探针目标写错 / 端口不对 / 服务在 gateway 后 | 探针默认打 `ws://127.0.0.1:${WS_PORT}${WS_PROBE_PATH}`；容器内需与 `--port` 一致；关掉探针用 `WS_PROBE_ENABLED=false`（此时该指标不出现） |
