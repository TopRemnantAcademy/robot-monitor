# 订单 31 — G-5：pgBackRest 真实备份收口（🟢）

> 触发：Issue #99 评论 `@codebuddy`（workMode=true）。
> 产出：分支 `feat/order-31-pgbackrest-real`，PR 待开。
> 基线：`main`。

## 0. 派单前提与实测的偏差（据实记录）

派单正文（Issue #99）称「`infra/ops/pgbackrest/` 只有配置契约；换带 pgbackrest 的镜像
或起边车」。**实测：前半句准确，后半句的「或」是架构误解** —— 见 §1 T1。

| 派单说法 | 实测 | 差异根因 |
| :--- | :--- | :--- |
| 只有配置契约、镜像层是裸 postgres | ✅ 准确：订单 25 已备好 `Dockerfile`/`Dockerfile.timescaledb`/overlay/两份 conf/`postgresql-dr.conf`/`run-backup.sh`/`restore.sh` | — |
| 「换镜像**或**起边车」二选一 | ❌ 必须是「派生镜像 **且** 仓库边车」 | pgBackRest 双端架构（§1） |
| 本仓无 Docker、无法验证 | ⚠️ 部分可破：容器**不能起**（无 user namespace），但 `docker compose config` 可跑；且可用 PGDG 包在本机跑**真 PG 集群**完成运行时验证 | 见 §3 |

**教训（补进 SOP 纪律第 7 条）**：凡引用「当前实现/当前状态」的派单，派前必须重跑
`ls`/`grep` 核对；凡断言「环境不支持」，先穷举替代验证路径（本例：compose config 与
本机真库都能跑）。

## 1. T1 方案定稿：为什么是「派生镜像 + 仓库边车」双端

订单 §T1 让在 (a) 换内置 pgbackrest 的镜像 与 (b) 官方镜像 + 边车 之间二选一。
**结论：二者不是替代关系，而是 pgBackRest 双端架构的两个必需半边。**

- **WAL 归档在 DB 主机本地执行**：`archive_command = 'pgbackrest … archive-push %p'`
  由 PostgreSQL 进程 fork 执行，**不可能**由仓库边车代劳。
  ⇒ DB 容器内必须有 pgbackrest ⇒ 必须 (a) 派生镜像（`Dockerfile.timescaledb`）。
- **仓库是独立主机**：备份落点、加密、retention 在仓库侧，需专职容器持有 repo 卷
  ⇒ 必须 (b) 边车（`Dockerfile` + `pgbackrest` 服务）。
- 两端**版本必须完全一致**：实证 `external/pgbackrest/doc/xml/user-guide.xml:2770`。
  两镜像均锁 **v2.59.3**（GitHub releases 实测为当前 stable，2026-10-04 发布）。

**淘汰「纯边车 + 把 PGDATA 挂进边车」**：会让两个容器争用同一份 PGDATA，与 PG 的
checkpoint 语义冲突，官方文档从未支持。详见 `docs/DR-RUNBOOK.md §2.1`。

**实测澄清**：`timescale/timescaledb:2.13.1-pg16` 底层是 **Alpine**（查 registry config
blob：`apk add libssl1.1`、BusyBox `adduser -S`、PG_VERSION=16.1），故
`Dockerfile.timescaledb` 的 Alpine 源码构建分支**方向正确**。补充发现：Alpine
`edge/community` 已有 `pgbackrest 2.59.3-r0`（与锁定版本一致），但那依赖 edge 分支，
本方案仍走源码构建（版本无关、可审计），并在注释中记录该备选。

## 2. 产出

| 文件 | 位置 | 内容 |
| :--- | :--- | :--- |
| `infra/ops/pgbackrest/backup.sh` | 新增 | T2 备份入口：`full/diff/incr/check/info/stanza-create`；幂等、失败非 0 |
| `infra/ops/pgbackrest/restore.sh` | 既有（保留） | T2 恢复入口：`latest/set/pitr`，两级确认 |
| `infra/ops/pgbackrest/verify.sh` | 新增 | T2 校验入口：L1 校验和 / L2 新鲜度 /（`--scratch`）L3 恢复比对 |
| `infra/ops/pgbackrest/verify-restore.sh` | 新增 | L3 承载：真恢复 + 行数/时间戳比对 |
| `infra/ops/pgbackrest/monitor.sql` | 新增 | T3：官方 `monitor.pgbackrest_info()` 范式 + 状态视图 |
| `infra/ops/pgbackrest/postgres-exporter-queries.yaml` | 新增 | T3：备份指标导出的自定义查询 |
| `infra/observability/rules/robot-alerts.yml` | 改 | 新增 `BackupStale`/`BackupErrored`/`BackupStanzaUnhealthy` |
| `infra/observability/docker-compose.observability.yml` | 改 | postgres-exporter 挂 `--extend.query-path` + 查询文件 |
| `infra/observability/verify_wiring.sh` | 改 | 增补备份指标接线自检 |
| `infra/ops/pgbackrest/docker-compose.dr.yml` | 改 | 挂载 5 个运维脚本 + `monitor.sql` |
| `infra/ops/pgbackrest/Dockerfile{,.timescaledb}` | 改 | 装 `jq`；构建期自检 `pgbackrest version` / `jq --version` |
| `infra/ops/pgbackrest/verify-drs.sh` | 改 | 扩到 5 脚本 + 禁 `|| true` 兜底 + compose 引用文件存在性 |
| `docs/DR-RUNBOOK.md` | 改 | T1 定稿 / T2 三入口 / T3 告警链 / §6 验证状态（实测证据 + 未验证项） |
| `infra/ops/pgbackrest/README.md` | 改 | 交付物表 + 校验两层 + 状态 |

## 3. 验收（全部实测，原文见 `docs/DR-RUNBOOK.md §6`）

- [x] `docker compose -f infra/ops/pgbackrest/docker-compose.dr.yml config` 退出码 0
- [x] 三层合并（基础 + DR overlay）config 退出码 0
- [x] 三入口 `--help` / 参数校验可跑；退出码语义明确
      （0 成功 / 2 用法错 / 3 前置条件缺失 / 其余透传；实测见 §6.2）
- [x] **真实全量备份成功**：`pgBackRest 2.59.3` + PKG 官方包，本机 PostgreSQL 16.15，
      `new backup label = 20261013-000357F`、`full backup size = 32.6MB`
- [x] **`verify.sh` L1+L2+L3 全绿**：仓库逐文件校验和 + 新鲜度断言 +
      真恢复到临时目录并比对 `robot_telemetry` 5000 行 / `max(time)` 一致
- [x] **负向用例**：篡改一份备份文件后 `verify.sh` 退出码 1（证明断言非假绿）
- [x] `promtool check rules`（v3.2.1）`SUCCESS: 12 rules found`
- [x] `verify-drs.sh` / `verify_wiring.sh` 全绿
- [x] `shellcheck -S warning` 五脚本零告警
- [x] `orders/31-dispatch.md` 一并提交进分支
- [x] 不破坏 CI：pytest / compileall 见 §4

## 4. 未验证（诚实边界，勿删）

容器编排层仍**未实跑**（本沙箱无 user namespace，容器起不来）：

| # | 未验证项 | 所需条件 |
| :--- | :--- | :--- |
| U1 | `Dockerfile.timescaledb`（Alpine 源码构建）构建 | 可跑 `docker build` 的机器 |
| U2 | `Dockerfile`（PGDG 安装）构建 | 联网 apt.postgresql.org |
| U3 | 三层合并 `up -d` 起栈 | Docker Compose v2 + 可用内核 |
| U4 | 双端 TLS 证书握手 | 生成证书（RUNBOOK §5.1）后跑 `backup.sh check` |
| U5 | RTO/RTO 实测计时 | 真实磁盘 + 造数据 |
| U6 | TimescaleDB **社区版** CAgg 在物理备份下的恢复 | 生产镜像（本机验证用 Apache 版，CAgg 不可用） |

本地实跑与容器环境的差异（版本/配置布局/TLS）已单列 `DR-RUNBOOK §6.4`。

## 5. 交付

- 分支 `feat/order-31-pgbackrest-real` → PR，描述贴 `docker compose config`
  与脚本 dry-run / 真实备份的原文输出。
- 禁止项自检：无 `|| true` 兜底（`verify-drs.sh` 门禁）、无 `echo OK` 式假验证
  （`verify.sh` 三层可证伪断言）、无明文密钥（`pgbackrest.conf` 僅 `${PGBACKREST_CIPHER_PASS}` 占位）。
