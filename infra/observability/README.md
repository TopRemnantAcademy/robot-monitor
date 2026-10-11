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
| `robot_backend_ws_probe_alive` 恒为 0 | 探针目标写错 / 端口不对 / 服务在 gateway 后 | 探针默认打 `ws://127.0.0.1:${WS_PORT}${WS_PROBE_PATH}`；容器内需与 `--port` 一致；关掉探针用 `WS_PROBE_ENABLED=false`（此时该指标不出现） |
