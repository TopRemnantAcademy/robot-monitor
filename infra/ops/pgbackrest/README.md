# pgBackRest 灾备边车（订单 25 / I-19）

> 完整运行手册见 **`docs/DR-RUNBOOK.md`**。本文件是目录导航 + 设计要点。

## 交付物

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 仓库(repository)主机的 pgBackRest 镜像（PGDG 包安装，v2.59.3） |
| `Dockerfile.timescaledb` | 派生 timescaledb 镜像：给基础镜像补 pgbackrest 二进制（`archive_command` 需要） |
| `docker-compose.dr.yml` | overlay：派生 timescaledb + pgbackrest 仓库边车 |
| `pgbackrest.conf` | **仓库侧**配置（含 `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}` 注入点） |
| `pgbackrest-db.conf` | **数据库侧**配置（不含口令；只有 `pg1-path` 等本机信息） |
| `postgresql-dr.conf` | PG 开关：`archive_mode=on` / `archive_command` / `archive_timeout`（经 `include_dir` 载入） |
| `run-backup.sh` | 备份调度入口（`full` / `diff` / `check` / `info`） |
| `restore.sh` | 恢复入口（`latest` / `set <name>` / `pitr '<ts>'`，带两级确认） |

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

## 状态

- ✅ 配置、脚本、compose、RUNBOOK 齐备；
- ✅ 静态自检通过（无需 Docker）：

  ```bash
  bash infra/ops/pgbackrest/verify-drs.sh
  ```

- ❌ **未端到端实跑**（本工作环境无 Docker）。未验证项与所需条件见
  `docs/DR-RUNBOOK.md §6`。
