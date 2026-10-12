# pgBackRest 灾备边车（订单 25 / I-19 建契约；订单 31 / G-5 收口为「真能备份」）

> 完整运行手册见 **`docs/DR-RUNBOOK.md`**。本文件是目录导航 + 设计要点。

## 交付物

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 仓库(repository)主机的 pgBackRest 镜像（PGDG 包安装，v2.59.3，含 `jq`） |
| `Dockerfile.timescaledb` | 派生 timescaledb 镜像：给基础镜像补 pgbackrest 二进制（`archive_command` 需要），含 `jq` |
| `docker-compose.dr.yml` | overlay：派生 timescaledb + pgbackrest 仓库边车（挂载全部 `/opt/dr/*.sh`） |
| `pgbackrest.conf` | **仓库侧**配置（含 `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}` 注入点） |
| `pgbackrest-db.conf` | **数据库侧**配置（不含口令；只有 `pg1-path` 等本机信息） |
| `postgresql-dr.conf` | PG 开关：`archive_mode=on` / `archive_command` / `archive_timeout`（经 `include_dir` 载入） |
| **`backup.sh`** | **T2 备份入口**（`full`/`diff`/`incr`/`check`/`info`/`stanza-create`；幂等、非 0 退出） |
| **`restore.sh`** | **T2 恢复入口**（`latest`/`set <name>`/`pitr '<ts>'`，带两级确认） |
| **`verify.sh`** | **T2 校验入口**：L1 校验和 + L2 新鲜度 +（`--scratch`）L3 恢复比对，**真断言** |
| **`verify-restore.sh`** | L3 承载：真实恢复 + 源库/恢复库 行数·时间戳比对 |
| **`monitor.sql`** | **T3 可观测**：官方 `monitor.pgbackrest_info()` 范式 → `monitor.pgbackrest_backup_status` 视图 |
| **`postgres-exporter-queries.yaml`** | **T3**：postgres-exporter 自定义查询，导出备份指标 |
| `run-backup.sh` | 订单 25 旧入口（保留，避免既有 runbook 失效） |
| `verify-drs.sh` | **静态**自检（YAML/结构/无明文密钥/脚本语法/无 `|| true` 兜底） |

## 为什么不能「只挂一个容器」就完事

pgBackRest 是双端架构，PG 主机与仓库主机**各自**都要有**同版本**二进制与配置：

- 实证 `external/pgbackrest/doc/xml/user-guide.xml:2770`
  > "The pgBackRest version installed on the repository host must **exactly match**
  > the version installed on the PostgreSQL host."
- WAL 归档靠 PG 本地执行的 `archive_command`，故 DB 容器内必须有 pgbackrest。

基础镜像 `timescale/timescaledb:2.13.1-pg16` **不含** pgbackrest —— 这正是本目录此前只交付
「配置契约」的原因。本次通过 `Dockerfile.timescaledb` 补上，使方案可用。

## ⚠️ 纠正：官方没有 `pgbackrest/pgbackrest` 镜像

`external/SOURCES.md:325` 记的 `pgbackrest/pgbackrest` **不存在**。2026-10-11 实测：

```
GET https://hub.docker.com/v2/repositories/pgbackrest/pgbackrest/tags
→ {"message":"object not found"}
```

`pgbackrest/*` 下只有 `test` / `doc` 两个仓库。故本方案自建镜像，走官方推荐的
PGDG 包仓库（`external/pgbackrest/doc/xml/user-guide.xml:586`）。

## 快速开始

```bash
# 1) 在宿主机 .env 配好仓库加密口令（禁止明文入库）
echo "PGBACKREST_CIPHER_PASS=$(openssl rand -base64 48)" >> .env

# 1.5) 生成双端 TLS 证书（compose 挂载 pgbackrest-certs 卷的前置条件）
#      完整命令见 docs/DR-RUNBOOK.md §5.1

# 2) 起带灾备的一整套
docker compose -f infra/docker-compose.yml \
               -f infra/ops/pgbackrest/docker-compose.dr.yml up -d --build

# 3) 初始化 stanza + 校验
DC="docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml"
$DC exec pgbackrest pgbackrest --stanza=robot_monitor --log-level-console=info stanza-create
$DC exec pgbackrest /opt/dr/run-backup.sh check

# 4) 首份全量
$DC exec pgbackrest /opt/dr/run-backup.sh full
```

## 校验怎么跑（两个层次，别混）

```bash
# ① 静态：文件形态对不对（无需 Docker，秒级）
bash infra/ops/pgbackrest/verify-drs.sh

# ② 运行时：备份到底可不可用（需要真实仓库，**有真断言**）
#    在 pgbackrest 仓库边车内执行：
/opt/dr/verify.sh              # L1 校验和 + L2 新鲜度
/opt/dr/verify.sh --scratch    # 追加 L3：真恢复到临时目录并比对行数/时间戳
```

① 只保证「配置不会因语法问题起不来」；**备份可用性只由 ② 判定**。

## 状态（订单 31 更新）

- ✅ 配置、T2 三入口脚本、compose、RUNBOOK、T3 告警接线齐备；
- ✅ 静态自检 + 指标接线自检通过：
  `verify-drs.sh` / `infra/observability/verify_wiring.sh`；
- ✅ **真实备份/恢复/校验已实跑**（本机 PG16 + pgBackRest 2.59.3，PGDG 官方包）：
  全量备份 → `verify.sh` L1+L2+L3 全绿（含恢复后行数/时间戳比对）→
  篡改文件后 `verify.sh` 正确变红（负向用例）。原文输出见 `docs/DR-RUNBOOK.md §6.2`；
- ⚠️ **容器编排层仍未实跑**（本工作环境容器不可起）：镜像构建、三层合并起栈、
  双端 TLS 握手、CAgg 恢复属未验证项，见 `docs/DR-RUNBOOK.md §6.3`。
