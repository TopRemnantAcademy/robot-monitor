# 灾备运行手册（DR RUNBOOK）— TimescaleDB

> 订单 **25 / I-19**。关联 `external/INTEGRATION-PLAN.md §8.9`（缺口 = 审计 #28）。
> 目标对象：`infra/docker-compose.yml` 的 `timescaledb` 服务（库对象见 `infra/init.sql`）。
> 工具选型：**pgBackRest**（选型理由见 §1）。

---

## 0. 一句话

100 台 × 10Hz = **1000 行/s ≈ 8.64M 行/天**（`infra/init.sql` 第 6 节注释实测口径）。
这套数据「丢了不可再生」—— 模拟器只会重放波形，历史折线一旦归零就永久缺失。
本手册给出：**备份怎么跑、坏到什么程度用哪条恢复路径、能丢多少数据（RPO）、要停多久（RTO）**。

---

## 1. 选型：pgBackRest 而非 wal-g

| 维度 | pgBackRest | wal-g | 本项判定 |
|---|---|---|---|
| 官方 Docker 镜像 | **无**（见下「纠正」） | 有 `wal-g/wal-g` | 平手（都要处理镜像来源） |
| 增量/块增量 | ✅ `--type=diff/incr` + `repo-block`(v2.46+) | ❌ 只做全量+WAL | **pgBackRest 胜** |
| 仓库加密 | ✅ `repo1-cipher-type=aes-256-cbc`（客户端侧） | 依赖外部 KMS | **pgBackRest 胜** |
| 并行/异步归档 | ✅ `process-max` / `archive-async` | 有限 | **pgBackRest 胜** |
| 恢复一致性校验 | ✅ `check` + `verify` | 依赖外部脚本 | **pgBackRest 胜** |
| 云对象存储 | ✅ repo1-type=s3/azure/gcs | ✅ 原生云优 | wal-g 略优 |

**结论**：本项目数据是「单机 PG + 需要分钟级 RPO + 需要增量」，pgBackRest 的增量与内置加密更契合。
wal-g 的优势集中在「云对象存储原生」，等真有 S3 仓库需求时可作为 repo2 并存（pgBackRest 支持多仓库）。

### ⚠️ 纠正一处旧记录（事实核对）

`external/SOURCES.md:325` 记的镜像是 `pgbackrest/pgbackrest` —— **该镜像不存在**。
2026-10-11 实测 Docker Hub：

- `https://hub.docker.com/v2/repositories/pgbackrest/pgbackrest/tags` → `{"message":"object not found"}`
- `pgbackrest/*` 下实际只有 `pgbackrest/test`、`pgbackrest/doc` 两个仓库

故本方案**自建镜像**（`infra/ops/pgbackrest/Dockerfile`），走官方唯一推荐的 PGDG 包仓库安装：

- 实证 `external/pgbackrest/doc/xml/user-guide.xml:586`：
  > "Ubuntu packages for pgBackRest are available at [apt.postgresql.org]"
- 安装方式实证同文件 `:343-344`：先 `apt-get install postgresql-common`，再执行
  `/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y`。

版本锁定 **v2.59.3**（`external/pgbackrest/README.md:7`："is the current stable release"）。

---

## 2. 架构：为什么是「双端」

pgBackRest 是**双端架构**：PostgreSQL 主机与仓库(repository)主机**各自**都要有
**完全相同版本**的 pgbackrest 二进制与配置。

> 实证 `external/pgbackrest/doc/xml/user-guide.xml:2770`：
> "The pgBackRest version installed on the repository host must **exactly match**
> the version installed on the PostgreSQL host."

而 PG 的 WAL 归档是本地执行的 `archive_command`（``pgbackrest ... archive-push %p``），
所以 **timescaledb 容器内必须有 pgbackrest 二进制**。基础镜像
`timescale/timescaledb:2.13.1-pg16` **不含**它，故：

```
┌─────────────────────────┐        ┌──────────────────────────┐
│ timescaledb (派生镜像)   │        │ pgbackrest (仓库边车)      │
│  timescale/...:2.13.1   │        │  robot-monitor-pgbackrest │
│  + pgbackrest 2.59.3    │        │  pgbackrest 2.59.3        │
│                         │        │                           │
│  archive_command ───────┼──WAL──▶│  repo1-path=/var/lib/...  │
│  (archive-push %p)      │  归档    │  repo1-cipher=aes-256-cbc │
│                         │        │                           │
│  pg1-path=/var/lib/...  │◀──全量──│  backup --type=full/diff  │
└─────────────────────────┘  备份   └──────────────────────────┘
         ▲                                      │
         │        pgbackrest-repo 卷（共享）      │
         └──────────────────────────────────────┘
```

- DB 侧配置：`infra/ops/pgbackrest/pgbackrest-db.conf`（**不含**加密口令 —— 加密在仓库侧完成）
- 仓库侧配置：`infra/ops/pgbackrest/pgbackrest.conf`（含 `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}`）
- 容器编排：`infra/ops/pgbackrest/docker-compose.dr.yml`（overlay）
- PG 侧开关：`infra/ops/pgbackrest/postgresql-dr.conf`（`archive_mode=on` 等，经 `include_dir` 载入）

---

## 3. RPO / RTO 目标（含计算过程，非拍脑袋）

### 3.1 基础参数（来自本仓实证）

| 参数 | 值 | 来源 |
|---|---|---|
| 写入速率 | 1000 行/s | `infra/init.sql` 第 6 节注释：100 台 × 10Hz |
| 行宽 | ≈1.34 KB/行 | 同上（逐列行宽 + 行头 24B + 页开销 1.3x） |
| 原始数据量 | ≈110 GiB/天 | 计算：1000×86400×1.34KB |
| WAL 生成 | ≈1x 数据量 | PG 保守估计（遥测为持续小事务写入） |
| 保留策略 | 遥测 30 天 / 告警 180 天 | `infra/init.sql` §6.2/§6.3 |
| 备份压缩 | zst ≈3x（保守） | pgBackRest `compress-type=zst`（user-guide.xml:1477 推荐 zst） |

### 3.2 RPO（能丢多少数据）

RPO 由「WAL 段被切出并成功推送到仓库」的时间决定：

- WAL 段大小默认 16 MiB；在 1.34 MB/s 的写入下，**段约 12 秒填满**即触发 `archive_command`；
- 夜间低谷期无写入 → 段不切 → 由 `archive_timeout=30s`（`postgresql-dr.conf`）强制切段兜底；
- 叠加网络推送延迟与 `archive-push-queue-max=4GiB` 缓冲（仓库短暂不可用时本地堆积、不丢）。

> **RPO 目标 = 1 分钟**（保守值；常态实测预期 ≈15~30 秒）。
> 依据：段填满 ≈12s + 推送延迟，低谷期被 `archive_timeout=30s` 封顶。

**前提**：仓库可用。若仓库不可用超过队列 4GiB 容量，pgBackRest 会**丢弃**该 WAL 段并只发警告
（实证 `external/pgbackrest/doc/xml/reference.xml:1699`），归档流中断 → RPO 失效、必须重做全量。

### 3.3 RTO（要停多久）

RTO 不是单一数字，**取两条路径中最坏的一条作为承诺**：

**路径 A — 数据目录丢失，整库从仓库重建**
仓库含 30 天数据全量 ≈ 1104 GiB（110GiB/天 ×30 ÷3 压缩）：

| 仓库读带宽 | 恢复耗时 |
|---|---|
| 100 MB/s（机械盘/跨网） | ≈188 min |
| 500 MB/s（NVMe 本地） | ≈38 min |
| 1000 MB/s（NVMe RAID） | ≈19 min |

**路径 B — 数据目录还在（逻辑损坏，如误删表），`--delta` 只补差异块**
近 7 天 chunk 差异 ≈ 258 GiB：

| 仓库读带宽 | 恢复耗时 |
|---|---|
| 100 MB/s | ≈44 min |
| 500 MB/s | ≈9 min |

> **RTO 目标：**
> - **常规目标（路径 B，本地 NVMe）≤ 20 分钟**（`--delta` 恢复 + WAL 回放 + promote）
> - **极端目标（路径 A，整库重建）≤ 4 小时**（100 MB/s 下限，必须实测仓库带宽后修订）

**诚实标注**：以上为**按容量的算术推算**，**未在真实环境计时**（本工作环境无 Docker，见 §6）。
首次数值必须由一次真实恢复演练替换。

---

## 4. 恢复步骤

> 命令均以 **pgbackrest 仓库边车**为执行环境。变量：`STANZA=robot_monitor`。

### 4.1 阶段零：判损（决定走 A 还是 B）

```bash
# 1) 集群还能起来吗？
docker compose -f infra/docker-compose.yml exec timescaledb pg_isready -U postgres -d robot_monitor
# 2) 能连就查关键对象是否完整
docker compose -f infra/docker-compose.yml exec timescaledb \
  psql -U postgres -d robot_monitor -c "SELECT count(*) FROM robot_metadata;"   # 期望 100
# 3) 数据目录物理文件是否健在？
docker compose -f infra/docker-compose.yml exec timescaledb ls /var/lib/postgresql/data/PG_VERSION
```

- 文件健在、仅逻辑损坏 → **路径 B**（§4.3）
- 数据目录整体丢失/损坏 → **路径 A**（§4.2）

### 4.2 路径 A：整库恢复（最新备份）

```bash
# ① 停应用 + 停 DB，避免恢复期间有写入
docker compose -f infra/docker-compose.yml stop backend alert-engine timescaledb

# ② 看清仓库里有什么（不恢复，只列表）
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec pgbackrest pgbackrest --stanza=robot_monitor info

# ③ 恢复（脚本内置两级确认；DR_FORCE_RESTORE=yes 是自动化开关）
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec -e DR_FORCE_RESTORE=yes pgbackrest /opt/dr/restore.sh latest

# ④ 起 DB，核对
docker compose -f infra/docker-compose.yml start timescaledb
docker compose -f infra/docker-compose.yml exec timescaledb pg_isready -U postgres -d robot_monitor
```

恢复动作语义实证：`restore` 会默认选最新备份，并自动写恢复所需的 `restore_command`
（`external/pgbackrest/doc/xml/reference.xml:2601`）。

### 4.3 路径 B：时间点恢复（PITR，误删/误改）

```bash
# ① 确定回滚目标时间（必须带时区；实证 user-guide.xml:2248 "include timezone offsets"）
#    目标时间应**早于**故障操作一两秒，避免边界命中
TARGET='2026-10-11 08:30:00+08'

# ② 停应用与 DB（同 §4.2 ① ），再执行 PITR
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec -e DR_FORCE_RESTORE=yes pgbackrest /opt/dr/restore.sh pitr "$TARGET"
```

`--type=time --target=<ts> --target-action=promote` 的语义与可行性实证：
`reference.xml:2601` restore 段的 allow-list（`time` 项）+ `user-guide.xml:2314` 的 PITR 示例。

### 4.4 恢复后一致性核对（必做，不得跳过）

```sql
-- 1) 基础对象
SELECT count(*) FROM robot_metadata;                      -- 期望 100
-- 2) 最近数据是否连续（无大段空洞）
SELECT max(time), min(time) FROM robot_telemetry WHERE time > now() - interval '1 hour';
-- 3) 连续聚合视图是否仍在刷新
SELECT * FROM timescaledb_information.jobs WHERE proc_name LIKE 'policy%';
-- 4) 告警表结构未回退（订单 19 的主键）
SELECT indexdef FROM pg_indexes WHERE tablename='robot_alert' AND indexname LIKE '%pkey%';
--     期望 PRIMARY KEY (time, alert_id)
```

### 4.5 备份触发（日常）

```bash
# 配置校验（新环境/改配置后必须先跑）
... exec pgbackrest /opt/dr/run-backup.sh check

# 全量（建议每周日 06:30）
... exec pgbackrest /opt/dr/run-backup.sh full

# 差异（建议周一~周六 06:30）
... exec pgbackrest /opt/dr/run-backup.sh diff
```

调度语义参考官方示例（`external/pgbackrest/doc/xml/user-guide.xml:1515-1520`）的 crontab 段。
容器环境用 cron / systemd timer / 编排器定时任务均可；关键是**全量周期 ≤ 保留窗口的一半**。

---

## 5. 凭证管理（禁止明文）

| 项 | 存放 | 机制 |
|---|---|---|
| 仓库加密口令 `repo1-cipher-pass` | **宿主机 `.env`** 的 `PGBACKREST_CIPHER_PASS` | 经 compose `environment` 注入容器为同名 `PGBACKREST_*` 变量，再由 pgbackrest 按官方规则读取 |
| 口令生成 | `openssl rand -base64 48` | 官方建议（`user-guide.xml` 的 configure-encryption 段原文） |

环境变量映射规则实证（`reference.xml:2127`）：

> "Any option may be set in an environment variable using the `PGBACKREST_` prefix
> and the option name in all caps replacing `-` with `_`"

即 `repo1-cipher-pass` → `PGBACKREST_REPO1_CIPHER_PASS`；
本方案刻意用 `PGBACKREST_CIPHER_PASS` 是**为了配置文件的 `${PGBACKREST_CIPHER_PASS}` 插值**，
最终仍映射到 `repo1-cipher-pass` 语义（配置里显式写成 `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}`）。

**护栏**：
- `.env` 已被 `.gitignore` 排除、被 `.dockerignore` 排除（`AGENTS.md §5`）；
- compose 用 `${PGBACKREST_CIPHER_PASS:?…}` —— 未设置时 **compose 直接报错退出**，
  而不是起一个空密钥仓库；
- 仓库内**任何文件不含明文密钥**；
- DB 侧配置 `pgbackrest-db.conf` **不持有**加密口令（加密只发生在仓库侧）。

### 5.1 生产加固：双端 TLS 证书（compose 挂载的前置条件）

`pgbackrest-db.conf` 与 `pgbackrest.conf` 都走 TLS 双端认证（容器间免 SSH 密钥分发），
证书卷 `pgbackrest-certs` 必须**先**生成，否则容器内 pgBackRest 会因读不到证书而失败。

```bash
# 一次性生成自签 CA + 双端证书（CN 必须与 tls-server-auth 白名单一致）
CERTDIR=./pgbackrest-certs   # 或 compose 卷 pgbackrest-certs 的实际落点
mkdir -p "$CERTDIR"; cd "$CERTDIR"

# CA
openssl req -new -x509 -days 3650 -nodes -subj "/CN=pgbackrest-ca"   -keyout ca.key -out ca.crt

# 仓库侧服务端证书（tls-server-cert-file）
openssl req -new -nodes -subj "/CN=pgbackrest-client" -keyout server.key -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial   -days 3650 -out server.crt

# DB 侧服务端证书（pgbackrest-db.conf 的 tls-server-cert-file）
openssl req -new -nodes -subj "/CN=pgbackrest-client" -keyout client.key -out client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial   -days 3650 -out client.crt

chmod 600 *.key
```

> 证书生成方式参考官方指引：`external/pgbackrest/doc/xml/user-guide.xml:2846`
> 「pgBackRest expects client/server certificates to be generated in the same way as
> PostgreSQL」（即用 openssl 生成自签 CA+书）。
>
> **说明**：官方按 CN 授权（`tls-server-auth=<CN>=<stanza>`，user-guide.xml:1859）。
> 本项目 configure 里 stdin 用了 `pgbackrest-client` 作为约定 CN，
> 实际部署请让**证书 CN 与配置里的白名单一致**，否则握手被拒。

---

## 6. 未验证清单（诚实边界）

本工作环境（CNB NPC 沙箱）**无 Docker**，故以下项**未实跑**，需在有 Docker 的环境中补齐：

| # | 未验证项 | 所需条件 | 验证方法 |
|---|---|---|---|
| U1 | 派生镜像 `Dockerfile.timescaledb` 能否构建 | `timescale/timescaledb:2.13.1-pg16` 的底层发行版（alpine vs debian）**未实测** | `docker build`；若报 `apk: not found` 改 debian 分支（Dockerfile 已注明） |
| U2 | `Dockerfile`（PGDG 安装 pgbackrest）能否构建 | 需联网访问 apt.postgresql.org | `docker build -f Dockerfile .`，看 `pgbackrest version` 输出 |
| U3 | overlay 与基础 compose 能否合并启动 | Docker Compose v2 | `docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml config` |
| U4 | `include_dir` 是否被 timescaledb 镜像的 PG 接受 | 运行时 | `SHOW include_dir;` + 确认 `archive_mode=on` 生效（`SHOW archive_mode;`） |
| U5 | `stanza-create` / `check` / `backup` 端到端 | 起容器 | `run-backup.sh check` 全绿 → `full` |
| U6 | **恢复演练（RTO 实测）** | 起容器 + 造数据 | 按 §4 走一遍并计时，用实测值替换 §3.3 |
| U7 | TimescaleDB 超表在物理备份下的恢复一致性 | 社区有讨论，本项目未实测 | 恢复后比对 `_timescaledb_catalog` 与 chunk 完整性 |
| U8 | 实测仓库读带宽（决定 RTO） | 真实磁盘 | `pgbackrest ... restore` 计时 |

> 订单 25 §2 T2 允许「若环境可跑附演练记录，否则贴『未验证+条件』」。
> 本节即该「未验证 + 条件」。**没有伪造任何演练记录**（订单 §4 禁止项 2）。

### 6.1 已完成的静态校验（可复现）

```bash
bash infra/ops/pgbackrest/verify-drs.sh
```

覆盖：compose YAML 解析、两份 pgbackrest.conf 结构、明文密钥扫描、脚本语法、shellcheck。
全部通过。**这只证明「配置不会因语法问题起不来」，不等于「备份能恢复」**——
后者必须走 U1~U6。
