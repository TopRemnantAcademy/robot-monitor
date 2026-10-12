# 灾备运行手册（DR RUNBOOK）— TimescaleDB

> 订单 **25 / I-19** 建立配置契约；订单 **31 / G-5** 收口为「真能备份」。
> 关联 `external/INTEGRATION-PLAN.md §8.9`（缺口 = 审计 #28）。
> 目标对象：`infra/docker-compose.yml` 的 `timescaledb` 服务（库对象见 `infra/init.sql`）。
> 工具选型：**pgBackRest**（选型理由见 §1）。

**订单 31 的关键变化（先读这条）**：订单 25 只交付了配置与脚本**契约**，镜像层仍是裸
postgres，仓库内没有任何一份真实备份。本单补齐了：

| 项 | 订单 25 | 订单 31 之后 |
|---|---|---|
| 备份入口 | `run-backup.sh`（full/diff/check/info） | **`backup.sh`**（+ incr / stanza-create，非 0 退出语义明确） |
| 恢复入口 | `restore.sh` | `restore.sh`（保留） |
| 校验入口 | `verify-drs.sh`（**静态**：YAML/语法） | **`verify.sh`**（L1 校验和 / L2 新鲜度 / L3 恢复比对）+ `verify-restore.sh` |
| 备份失败告警 | 无 | `monitor.sql` + postgres-exporter 自定义查询 + `BackupStale/BackupErrored` 规则（复用 #59 通道） |
| 端到端证据 | 无（环境无 Docker） | **真实 PG16 + pgBackRest 2.59.3 实跑**，见 §6 |

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

## 2. 架构与 T1 方案定稿（订单 31）

### 2.1 T1 二选一：为什么最终是「派生镜像 + 仓库边车」两者都要

订单 31 §T1 让在 (a) 换内置 pgbackrest 的 postgres 镜像 与 (b) 官方镜像 + pgbackrest
边车 之间二选一。**结论：(a) 与 (b) 不是替代关系，而是双端架构的两个必需半边**，
必须同时具备 —— 这不是"两个都选"，而是 pgBackRest 的架构决定的：

- **WAL 归档在 DB 主机本地执行**：`archive_command = 'pgbackrest … archive-push %p'`
  是 PostgreSQL 进程自己 fork 执行的命令，**不可能**由仓库边车代替
  （边车不在 PG 的进程空间里）。⇒ **DB 侧必须有 pgbackrest 二进制** ⇒ 需要 (a) 派生镜像。
- **仓库（备份落点）是独立主机**：备份数据、加密、retention 都在仓库侧，
  必须有一个专职容器持有 `pgbackrest-repo` 卷 ⇒ 需要 (b) 边车。
- 且两端**版本必须完全一致**：user-guide.xml:2770
  > "The pgBackRest version installed on the repository host must exactly match …"

所以本方案 = `Dockerfile.timescaledb`（在 timescaledb 镜像上补 pgbackrest）**且**
`Dockerfile` + `pgbackrest` 边车。二者都锁定 **v2.59.3**（§1 已核对为当前 stable）。

**为什么不选「纯 (b) 边车 + 把 PGDATA 挂进边车」**：那条路要求边车直接对 PG 的
data directory 做物理读写，同时还要让 PG 自己去 archive，等于让两个容器争用同一份
PGDATA —— 官方文档从未支持这种布局，且会与 PG 的 checkpoint 语义冲突。淘汰。

### 2.2 为什么是「双端」

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

> 订单 31 起，这一步**已自动化**：`verify.sh --scratch` / `verify-restore.sh` 会真实恢复
> 到临时目录、启动只读实例，并比对行数 / 最新时间戳（见 §4.6）。下列 SQL 是人工排查时的等价手段。

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

### 4.5 备份触发（日常，订单 31 §T2 三入口）

`/opt/dr/` 下有三个入口脚本（**都是幂等的，失败一律非 0 退出**）：

| 脚本 | 用途 | 关键退出码 |
|---|---|---|
| `backup.sh` | `full` / `diff` / `incr` / `check` / `info` / `stanza-create` | 0 成功；2 用法错；3 前置条件缺失；其余透传 pgbackrest |
| `restore.sh` | `latest` / `set <name>` / `pitr '<ts>'` | 需 `DR_FORCE_RESTORE=yes` 才动手（两级确认） |
| `verify.sh` | L1 校验和 + L2 新鲜度（+ `--scratch` 时 L3 恢复比对） | 0 通过；1 断言失败；2 用法错；3 前置条件缺失 |

```bash
# 0) 新环境：先建 stanza（幂等）
... exec pgbackrest /opt/dr/backup.sh stanza-create

# 1) 配置/归档链路校验（改配置后必须先跑这条）
... exec pgbackrest /opt/dr/backup.sh check

# 2) 全量（建议每周日 06:30）
... exec pgbackrest /opt/dr/backup.sh full

# 3) 差异（建议周一~周六 06:30）
... exec pgbackrest /opt/dr/backup.sh diff

# 4) 备份后自证：只有 verify.sh 通过，才算"这份备份可用"
... exec pgbackrest /opt/dr/verify.sh
#    需要真恢复比对时（更强，耗时更长）：
... exec pgbackrest /opt/dr/verify.sh --scratch
```

**调度建议**（cron 语义，实证 `external/pgbackrest/doc/xml/user-guide.xml:1515-1520` 的 crontab 段）：

```cron
#  m   h  dom mon dow  command
   30  06   *   *   0   /opt/dr/backup.sh full
   30  06   *   *  1-6  /opt/dr/backup.sh diff
   00  07   *   *   *   /opt/dr/verify.sh                 # 每日校验新鲜度
   00  03   *   *   0   /opt/dr/verify.sh --scratch       # 每周真恢复演练
```

容器环境用 cron / systemd timer / 编排器定时任务均可。
**关键约束**：全量周期 ≤ 保留窗口的一半（§4.7 保留策略）。

### 4.6 备份真实性校验（订单 31 §T2：`verify.sh` 三层断言）

`verify.sh` **不是 echo OK**，它逐层用可证伪的断言判定：

- **L1 仓库一致性**：`pgbackrest verify --verbose` 输出里
  - stanza 段必须 `status: ok`
  - 每份备份必须 `status: valid`
  - 不得出现 `invalid checksum` / `missing: N>0` / `size invalid: N>0` / `other: N>0`
  > ⚠️ **实测陷阱（2.59.3）**：`verify` 检出校验和不符时**进程退出码仍是 0**！
  > 只判退出码 = 假绿。故本层解析输出文本（见脚本注释）。
- **L2 备份新鲜度**：解析 `info --output=json`（官方稳定接口），断言
  - `status.code == 0`
  - 至少 1 份全量备份
  - 最新备份 `error == false`
  - `now - timestamp.stop <= 阈值`（默认 86400s，可用 `--max-age-seconds` 调）
  - WAL 归档区间非空（`archive[-1].min/max` 均存在）
- **L3 恢复真实性**（`--scratch`）：真实 restore 到临时目录 → 启动只读实例 →
  **比对源库与恢复库的 `count(*)` 与 `max(time)`**（默认表：robot_telemetry /
  robot_metadata / robot_alert）。环境不具备时打印明确 skip 原因并以 0 退出。

```bash
# 加阈值：超过 2h 没有新备份即失败
... exec pgbackrest bash -c 'PGBACKREST_MAX_BACKUP_AGE_SECONDS=7200 /opt/dr/verify.sh'
```

### 4.7 保留策略（与 `pgbackrest.conf` 一致）

| 项 | 值 | 配置键 |
|---|---|---|
| 全量保留 | 2 份（count 语义） | `repo1-retention-full=2` |
| 差异保留 | 6 份 | `repo1-retention-diff=6` |
| 仓库加密 | aes-256-cbc | `repo1-cipher-type`（§5） |
| WAL 归档队列上限 | 4GiB（仓库不可用时本地堆积不丢） | `archive-push-queue-max` |

> count 语义实证 `reference.xml:944`（`repo-retention-full-type`）：`count` 时
> 「超出的全量才被过期」；`time` 时才按天。默认 `count`。
> 约束：全量周期 ≤ 保留窗口一半 —— 保留 2 份全量 ⇒ 全量间隔 ≤ 1 周（与 §4.5 cron 一致）。

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

### 5.2 备份失败的告警接入（订单 31 §T3，复用 #59 既有通道）

**不另造一套告警通道**：`#59` 已把「告警落库失败」做成
`robot_alert_store_errors_total` → Prometheus 规则 `AlertStoreFailing` →
Alertmanager(`infra/observability/alertmanager.yml`) → webhook。
备份失败复用同一条链，只补一个**指标来源**：

```
pgbackrest --output=json info         (官方稳定接口)
        │  COPY FROM PROGRAM（官方 user-guide.xml Monitoring 示例）
        ▼
monitor.pgbackrest_info()  →  view monitor.pgbackrest_backup_status
        │  infra/ops/pgbackrest/monitor.sql
        ▼
postgres-exporter --extend.query-path=postgres-exporter-queries.yaml
        ▼
pgbackrest_backup_last_success_timestamp_seconds / _last_errored / pgbackrest_stanza_status_code
        ▼
infra/observability/rules/robot-alerts.yml：BackupStale / BackupErrored / BackupStanzaUnhealthy
        ▼
Alertmanager → robot-monitor-webhook（与 AlertStoreFailing 同一条）
```

落地三步：

```bash
# ① DB 侧建函数与视图（一次即可；见 monitor.sql，官方示例范式）
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec timescaledb psql -U postgres -d robot_monitor -f /opt/dr/monitor.sql

# ② 确认视图能读到仓库状态（返回一行：status_code=0 即健康）
... exec timescaledb psql -U postgres -d robot_monitor \
  -tAc 'SELECT * FROM monitor.pgbackrest_backup_status'

# ③ 把自定义查询挂给 postgres-exporter（observability overlay 已配好）
docker compose -f infra/docker-compose.yml -f infra/observability/docker-compose.observability.yml up -d
curl -s localhost:9187/metrics | grep pgbackrest_
```

三条规则的语义边界：

- `BackupStale`：`time() - pgbackrest_backup_last_success_timestamp_seconds > 93600`（26h）。
  **周期性**事件，判据是"新鲜度"，不是瞬时 `> 0`。
- `BackupErrored`：`pgbackrest_backup_last_errored > 0`。最新那份备份不完整
  （≠ 没有备份，而是"最新那份不可用于恢复"）。
- `BackupStanzaUnhealthy`：`pgbackrest_stanza_status_code > 0`。
  stanza 路径丢失/配置错 —— 此时 `backup.sh` 本身就会失败。

> ⚠️ 上游状态：`--extend.query-path` 在 postgres_exporter 已标记 DEPRECATED
> （README「Adding new metrics via a config file (DEPRECATED)」段），v0.17.1 仍支持。
> 若将来被移除，迁移点只有一处（视图 `monitor.pgbackrest_backup_status` 不变）。

---

## 6. 验证状态（订单 31 更新：从「未验证」到「有真实证据」）

### 6.1 环境与版本（实测）

订单 31 的验收在 **本机真实 PG 集群**上完成（不是 mock）：用官方 PGDG 仓库
（与 `Dockerfile` 相同的安装路径）装了 `postgresql-16` + `pgbackrest`：

```
$ pgbackrest version
pgBackRest 2.59.3          # 与 pgbackrest.conf / Dockerfile 锁定版本一致

$ /usr/lib/postgresql/16/bin/postgres --version
postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2)
```

> 为什么这算"真"验证：`pgbackrest` 是官方包、版本就是仓库锁的 2.59.3；
> 备份/恢复/校验走的是 **同一条 pgBackRest 代码路径**（§4 / §5 的命令逐字执行），
> 只是把「容器」换成了「本机进程」。**容器编排层**另有未验证项（§6.4）。

### 6.2 已完成并通过的实跑（附原文输出）

**① 真实全量备份 + 校验**（`backup.sh`）：

```
$ backup.sh full
INFO: backup start archive = 000000010000000000000002, lsn = 0/2000028
INFO: new backup label = 20261013-000357F
INFO: full backup size = 32.6MB, file total = 1379
INFO: backup command end: completed successfully (4601ms)

$ backup.sh check      # 触发 pg_switch_wal 并确认所需 WAL 已到仓库
… completed successfully
```

**② 三层真实性校验**（`verify.sh --scratch`，退出码 0）：

```
== L1 仓库一致性（pgbackrest verify：逐文件校验和）==
L1a OK：verify 报告 stanza status: ok
L1b OK：verify 逐份备份 status: valid（共 4 份）
L1c OK：无 invalid/missing/checksum/other 异常项
== L2 备份新鲜度与完整性（断言 info JSON）==
L2a OK：stanza status.code=0（ok）
L2b OK：仓库内共 2 份备份，其中全量 2 份（retention 基准存在）
L2c OK：最新备份 20261013-000617F 无错误（error=false）
L2d OK：最新成功备份距今 359s ≤ 阈值 86400s
L2e OK：WAL 归档区间非空 [000000010000000000000006 .. 00000001000000000000000C]
== L3 恢复真实性（真恢复到临时目录 + 行数/时间戳比对）==
基线 robot_telemetry：count=5000 max_time=2026-10-13 00:03:54.696285+08
基线 robot_metadata：count=100
恢复产物校验：PG_VERSION=16，文件数=1381
OK 表 robot_telemetry 行数一致：5000
OK 表 robot_telemetry max(time) 一致：2026-10-13 00:03:54.696285+08
OK 表 robot_metadata 行数一致：100
恢复库 timescaledb=2.30.2，chunk 数=1
恢复真实性全部通过：源库与恢复库 行数 / 最新时间戳 一致。
全部断言通过（L1 + L2 + L3）。        # 退出码 0
```

**③ 负向用例：断言真的会失败**（否则就是假绿）。篡改仓库内一份备份文件的一个字节块：

```
$ verify.sh
L1a FAIL：verify 未报告 stanza status: ok
L1c FAIL：verify 检出无效文件 —— 仓库已损坏，备份不可用于恢复
存在未通过断言。                        # 退出码 1

# 恢复被篡改的文件后
$ verify.sh … 退出码 0（回到全绿）
```

> 这条负向用例同时暴露了一个**真实陷阱**：`pgbackrest verify` 检出
> `invalid checksum` 时**进程退出码仍是 0**。故 `verify.sh` 的 L1 解析输出文本，
> 而不是只判退出码 —— 只判退出码的实现会在仓库损坏时给出假绿。

**④ 备份状态可观测**（`monitor.sql` → postgres-exporter → 规则）：

```
$ psql -d robot_monitor -tAc 'SELECT * FROM monitor.pgbackrest_backup_status'
robot_monitor|0|1791821181|f|00000001000000000000000C
                │        └ last_backup_stop (epoch 1791821181 = 2026-10-13 00:06:21)
                └ status_code=0（ok）
```

Prometheus 规则经官方 `promtool` 校验（版本 v3.2.1，与 compose 锁定一致）：

```
$ promtool check rules infra/observability/rules/robot-alerts.yml
SUCCESS: 12 rules found      # 含新增的 BackupStale / BackupErrored / BackupStanzaUnhealthy
```

**⑤ 静态与门禁校验**（可复现）：

```bash
bash infra/ops/pgbackrest/verify-drs.sh          # 文件形态：YAML/结构/无明文密钥/脚本语法
bash infra/observability/verify_wiring.sh        # 备份指标接线：规则引用 ↔ 来源 ↔ exporter 挂载
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml config
docker compose -f infra/ops/pgbackrest/docker-compose.dr.yml config
```

前三条均通过（compose config 退出码 0；见附录 A 的完整输出）。`verify-drs.sh`
**只证明「配置不会因语法问题起不来」**，备份可用性由 `verify.sh` 判定 —— 二者分工已在脚本头注明。

### 6.3 未验证清单（诚实边界，勿删）

以下项**没有**在本环境实跑，因为它们依赖「真实容器编排层」或「真实磁盘带宽」：

| # | 未验证项 | 所需条件 | 验证方法 |
|---|---|---|---|
| U1 | `Dockerfile.timescaledb`（Alpine 源码构建 pgbackrest）能否构建 | 能跑 Docker build 的机器 | `docker build -f Dockerfile.timescaledb`；构建期已内置 `pgbackrest version` 自检 |
| U2 | `Dockerfile`（PGDG 安装）能否构建 | 联网访问 apt.postgresql.org | `docker build -f Dockerfile .`；构建期已内置 `pgbackrest version` + `jq --version` 自检 |
| U3 | overlay 三层合并后容器能否真正起栈 | Docker Compose v2 + 可用内核 | `docker compose … up -d`（本地已用 `config` 判定配置层可解析，见附录 A） |
| U4 | `include_dir` 被 timescaledb 的 PG 接受 | 运行时 | `SHOW include_dir;` / `SHOW archive_mode;` |
| U5 | 双端 TLS 证书握手 | 运行时 | 生成证书（§5.1）后跑 `backup.sh check` |
| U6 | **RTO 实测计时** | 真实磁盘 + 造数据 | 按 §4.2/§4.3 走一遍并计时，用实测值替换 §3.3 的算术推算 |
| U7 | 仓库读带宽实测 | 真实磁盘 | `pgbackrest … restore` 计时 |
| U8 | TimescaleDB 连续聚合（CAgg）在物理备份下的恢复 | 需社区版 timescaledb | 本地验证用的是 Apache 版（CAgg 不可用，故 `init.sql` 的 CAgg 段未覆盖）；生产镜像为社区版，需在容器内复核 |

> **没有伪造任何演练记录**（订单 31 §禁止项 / 订单 25 §4 禁止项 2）。
> 本地实跑的证据（§6.2）是**真实命令的真实输出**；容器层的缺口单列在 §6.3。

### 6.4 本地实跑与容器环境的差异（必须知道）

| 维度 | 本地验证 | 生产（compose） |
|---|---|---|
| pgbackrest 版本 | 2.59.3（PGDG 包） | 2.59.3（PGDG 包 / Alpine 源码构建），**一致** |
| PG 版本 | 16.15 | 16（timescaledb 2.13.1-pg16 内置 16.1），**主版本一致** |
| timescaledb 扩展 | 2.30.2（Apache 版，PGDG） | 2.13.1（社区版，镜像内置），**版本不同** |
| `postgresql.conf` 位置 | Debian split-config（`/etc/postgresql/16/main/`） | PGDATA 内（`/var/lib/postgresql/data/`） |
| 双端认证 | 本机 Unix socket | TLS 双端证书（`pg1-host-type=tls`） |

**结论**：备份/恢复**代码路径**已验证；**容器编排 + TLS + CAgg** 属 §6.3 未验证项。
timescaledb 扩展版本差异不影响物理备份本身的正确性（备份的是文件与 WAL），
但 CAgg 相关的恢复断言需在生产镜像内复核（U8）。

---

## 附录 A：可复现的判定命令与原文输出（订单 31 验收）

### A.1  compose config（T1 可判定判据）

```console
$ PGBACKREST_CIPHER_PASS=demo docker compose \
    -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml config
name: infra
  emqx:
    image: emqx/emqx:5.6
        target: 1883
        target: 18083
  pgbackrest:
      dockerfile: Dockerfile
    image: robot-monitor-pgbackrest:local
        target: /var/lib/pgbackrest
        source: /workspace/infra/ops/pgbackrest/pgbackrest.conf
        target: /etc/pgbackrest/pgbackrest.conf
        target: /var/log/pgbackrest
        source: /workspace/infra/ops/pgbackrest/backup.sh
        target: /opt/dr/backup.sh
        source: /workspace/infra/ops/pgbackrest/restore.sh
        target: /opt/dr/restore.sh
        source: /workspace/infra/ops/pgbackrest/verify.sh
        target: /opt/dr/verify.sh
        source: /workspace/infra/ops/pgbackrest/verify-restore.sh
        target: /opt/dr/verify-restore.sh
        source: /workspace/infra/ops/pgbackrest/run-backup.sh
        target: /opt/dr/run-backup.sh
        target: /etc/pgbackrest/cert
  redis:
    image: redis:7.2-alpine
        target: 6379
  timescaledb:
      dockerfile: Dockerfile.timescaledb
    image: robot-monitor-timescaledb:p16-pgbackrest
        target: 5432
        target: /var/lib/postgresql/data
        target: /docker-entrypoint-initdb.d/init.sql
        source: /workspace/infra/ops/pgbackrest/pgbackrest-db.conf
        target: /etc/pgbackrest/pgbackrest.conf
        target: /var/lib/pgbackrest
        source: /workspace/infra/ops/pgbackrest/postgresql-dr.conf
        target: /etc/postgresql/conf.d/zz-dr.conf
        target: /var/log/pgbackrest
        target: /etc/pgbackrest/cert
  default:
... (退出码 0；完整 200+ 行省略，可本地重跑得到)
```

单独解析 DR overlay 文件（订单 31 §验收标准第 1 条的字面命令）：

```console
$ PGBACKREST_CIPHER_PASS=demo docker compose -f infra/ops/pgbackrest/docker-compose.dr.yml config
name: pgbackrest
services:
  pgbackrest:
    build:
      context: /workspace/infra/ops/pgbackrest
      dockerfile: Dockerfile
    command:
      - -c
      - sleep infinity
    container_name: robot-monitor-pgbackrest
    depends_on:
      timescaledb:
# 退出码 0
```

### A.2  T2 三入口 --help / dry-run（无真实库时的 skip 语义）

```console
$ /opt/dr/backup.sh --help      # 退出码 0
$ /opt/dr/backup.sh bogus       # 退出码 2（未知模式，打印可选值）
$ /opt/dr/backup.sh full        # 退出码 3（stanza 不存在时，打印"先跑 stanza-create"）
$ /opt/dr/verify.sh             # 退出码 0（L1+L2 通过）/ 1（断言失败）/ 3（前置缺失）
$ /opt/dr/verify.sh --scratch   # 追加 L3 真恢复比对；环境不具备时打印 skip 原因并退出 0
```

### A.3  负向用例（证明断言非假绿）

篡改仓库内一份备份文件后：

```console
$ verify.sh
L1a FAIL：verify 未报告 stanza status: ok
L1c FAIL：verify 检出无效文件 —— 仓库已损坏，备份不可用于恢复
# 退出码 1
```
