# 订单 25 — I-19 灾备（🟢）

> 关联：INTEGRATION-PLAN §8.9；源 pgbackrest 或 wal-g。
> 触发方式：post-issue-comment（本文件不含触发词）。

## 0. 现状

- **缺口**：无数据库灾备方案（审计 #28）。一旦 timescaledb 数据损坏/丢失，无法恢复。
- **判据**：RPO/RTO 量化 + 恢复演练记录。
- **状态**：未验证。

## 1. 必读文件

| 顺序 | 文件 | 目的 |
|---|---|---|
| 1 | `infra/docker-compose.app.yml` | timescaledb service 与卷定义（备份目标） |
| 2 | `external/pgbackrest/README.md` | 部署范式（或 wal-g） |
| 3 | `infra/init.sql` | 需备份的库对象 |

## 2. 任务

### T1 — 选型与配置
- 在 `infra/` 下加灾备 service（pgbackrest 或 wal-g），对 timescaledb 做定期全量 + WAL 归档。
- 配置 repo 路径（可用挂载卷或对象存储占位），明确 RPO/RTO 目标值。

### T2 — 恢复演练文档
- 写 `docs/DR-RUNBOOK.md`：备份触发、恢复步骤、RPO/RTO 计算。
- 若环境可跑，附一次恢复演练记录（否则贴「未验证 + 条件」）。

## 3. 验收标准

- [ ] 灾备 service 配置进 compose（或独立 yaml），目标库指向 timescaledb
- [ ] `docs/DR-RUNBOOK.md` 存在，含 RPO/RTO 数值与恢复步骤
- [ ] 明确「未验证」部分（无对象存储/无演练环境时）

## 4. 禁止项

1. 禁止把备份凭证硬编码明文
2. 禁止伪造恢复演练记录
3. 禁止只推分支就宣称完成

## 5. 交付方式

1. 分支 `feat/order-25-dr`
2. 尝试 push + 开 PR

## 6. 失败上报

- 无对象存储/演练环境时写「配置完成，演练未验证」
- 选型理由写明（pgbackrest vs wal-g）

## 7. 定位一句话

你是验收方，证据优先：灾备必须有可量化 RPO/RTO 与恢复步骤，不能只挂个容器。
