# 订单 21 — I-11 TimescaleDB 调优 env 补进 docker-compose（🔴）

> 关联：external/INTEGRATION-PLAN.md §8.1；上游源 external/timescaledb-tune + timescaledb 官方镜像 README。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **缺口**：`infra/docker-compose.yml`（或 `infra/docker-compose.app.yml`，以实际存在者为准，先 grep 确认）的
  timescaledb service `environment` 里**一个 `TS_TUNE_*` 都没设**（实证据：本仓全 grep `TS_TUNE` 无命中）。
- **危害**：压测基线不可复现（R-08 C1 要求 ±10%），同一套硬件两次压测数字对不上。
- **源已下载**：`external/timescaledb-tune`（前轮已下）。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `infra/docker-compose.yml` 或 `infra/docker-compose.app.yml`（先 grep 确认哪个存在） | 当前 timescaledb service 定义与网络名 |
| 2 | `external/timescaledb-tune/README.md` | `TS_TUNE_*` 可用变量清单与语义 |
| 3 | `external/timescaledb/README.md` | 官方镜像的环境变量约定 |

## 2. 任务（T1 唯一）

- 在 timescaledb service 的 `environment` 增加一组 `TS_TUNE_*` / `TIMESCALEDB_*` 调优变量，
  至少覆盖：`shared_buffers`、`max_connections`、`effective_cache_size`、
  `checkpoint_completion_target`、`max_wal_size`、`random_page_cost`、`wal_compression`。
- 取值参考 timescaledb-tune 在 100 台 / 16GB 类规格的推荐（文档有则照引，无则按保守默认并注明来源）。
- **仅改 compose 环境变量，不要改 init.sql / 不改业务代码**。

## 3. 验收标准

- [ ] `grep -n "TS_TUNE\|TIMESCALEDB_" infra/docker-compose*.yml` 有命中（≥5 项）
- [ ] `docker compose config` 解析通过（若 NPC 环境有 docker）；无 docker 则贴「未验证 + 条件」
- [ ] 每个变量名能在 timescaledb-tune / 官方 README 找到出处，非臆造

## 4. 禁止项

1. 禁止改 init.sql / 业务代码（本单只动 compose env）
2. 禁止臆造不存在的 TS_TUNE 变量名（每个变量必须能在 README 找到出处）
3. 禁止只推分支就宣称完成；push 失败原文报告

## 5. 交付方式

1. 分支 `feat/order-21-tsdb-tune-env`
2. 尝试 push + 开 PR

## 6. 失败上报

- 无 docker 时明确写「配置语义完成，运行时未验证」
- 变量名找不到出处时列出已查的 README 章节

## 7. 定位一句话

你是验收方，证据优先：每个 env 变量必须给出 README 出处。
