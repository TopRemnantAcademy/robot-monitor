# 订单 22 — I-12 观测三件套接进容器网络（🔴）

> 关联：INTEGRATION-PLAN §8.2；源 postgres_exporter(README:252)、redis_exporter(README:197,216)。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **缺口**：`prometheus/client_python` 已下但 `grep -rn prometheus backend_ingestion` 无业务输出；
  `postgres_exporter` / `redis_exporter` 尚未作为 service 接入。
- **关键坑（前轮同坑）**：exporter 必须与 backend / redis / timescaledb **同网络**，
  否则 `DATA_SOURCE_NAME` 解析不到目标。
- **状态**：未验证（无 Docker）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `infra/docker-compose.app.yml` | 现有服务与网络定义（确认网络名） |
| 2 | `external/postgres_exporter/README.md:252` | `DATA_SOURCE_NAME` 写法 |
| 3 | `external/redis_exporter/README.md:197,216` | redis 指标暴露端口/变量 |
| 4 | `backend_ingestion/app/main.py:133` | `/metrics` 端口，确认 exporter 能拉到 |

## 2. 任务

### T1 — 新建观测 compose
- 新建 `infra/observability/docker-compose.observability.yml`，加 `postgres_exporter` + `redis_exporter` 两个 service。
- 两 service 必须挂到与 backend / redis / timescaledb **相同的 network**（复用现有网络名，不要新建隔离网）。

### T2 — 接线变量
- postgres_exporter：`DATA_SOURCE_NAME=postgresql://<user>:<pw>@<timescaledb-host>:<port>/<db>`（密码走 env / secret，禁止硬编码明文进仓库）。
- redis_exporter：默认 `redis://<redis-host>:6379`。

### T3 — 核对 app /metrics
- 确认 backend_ingestion 的 `/metrics`（prometheus-client 暴露）端口，并补一个 `prometheus` scrape 配置片段（或说明接入现有 prometheus）。

## 3. 验收标准

- [ ] `infra/observability/docker-compose.observability.yml` 存在，两 exporter service 在同一 network
- [ ] `DATA_SOURCE_NAME` 用变量引用，无明文密码入库
- [ ] `docker compose -f ... config` 通过（有 docker 时）；无 docker 贴「未验证」

## 4. 禁止项

1. 禁止把 exporter 放到隔离网络（必须同网，否则拉不到）
2. 禁止硬编码数据库密码明文
3. 禁止改 backend 业务代码（本单只加观测 sidecar）
4. 禁止只推分支就宣称完成

## 5. 交付方式

1. 分支 `feat/order-22-observability-exporters`
2. 尝试 push + 开 PR

## 6. 失败上报

- 无 Docker 时写「配置语义完成，运行时未验证」
- network 名若与现有不一致，列出实际网络名与证据

## 7. 定位一句话

你是验收方，证据优先：exporter 同网是硬约束，必须贴 compose network 配置证明。
