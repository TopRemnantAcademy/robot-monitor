# pgBackRest disaster-recovery sidecar (Order 25 / I-19 establishes the contract; Order 31 / G-5 closes it as 'real backups')

> Full runbook: **`docs/DR-RUNBOOK.md`**. This file is a directory map + design highlights.

## Deliverables

| File | Purpose |
|---|---|
| `Dockerfile` | pgBackRest image for the repository host (PGDG package, v2.59.3, includes `jq`) |
| `Dockerfile.timescaledb` | derived timescaledb image: adds the pgbackrest binary to the base image (needed by `archive_command`), includes `jq` |
| `docker-compose.dr.yml` | overlay: derived timescaledb + pgbackrest repository sidecar (mounts all `/opt/dr/*.sh`) |
| `pgbackrest.conf` | **repository-side** config (with `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}` injection point) |
| `pgbackrest-db.conf` | **database-side** config (no password; only local info like `pg1-path`) |
| `postgresql-dr.conf` | PG switches: `archive_mode=on` / `archive_command` / `archive_timeout` (loaded via `include_dir`) |
| **`backup.sh`** | **T2 backup entry** (`full`/`diff`/`incr`/`check`/`info`/`stanza-create`; idempotent, non-zero exit on failure) |
| **`restore.sh`** | **T2 restore entry** (`latest`/`set <name>`/`pitr '<ts>'`, with two-level confirmation) |
| **`verify.sh`** | **T2 verification entry**: L1 checksum + L2 freshness + (`--scratch`) L3 restore comparison, with **real assertions** |
| **`verify-restore.sh`** | L3 carrier: real restore + source/restore DB row-count · timestamp comparison |
| **`monitor.sql`** | **T3 observability**: official `monitor.pgbackrest_info()` pattern → `monitor.pgbackrest_backup_status` view |
| **`postgres-exporter-queries.yaml`** | **T3**: postgres-exporter custom queries exporting backup metrics |
| `run-backup.sh` | Order 25 legacy entry (kept to avoid breaking the existing runbook) |
| `verify-drs.sh` | **static** self-check (YAML/structure/no plaintext secrets/script syntax/no `|| true` fallback) |

## Why you can't just 'mount one container' and call it done

pgBackRest is a two-end architecture; the PG host and the repository host **each** need the **same-version** binary and config:

- Evidence `external/pgbackrest/doc/xml/user-guide.xml:2770`
  > "The pgBackRest version installed on the repository host must **exactly match**
  > the version installed on the PostgreSQL host."
- WAL archiving relies on PG's locally-executed `archive_command`, so the DB container must contain pgbackrest.

The base image `timescale/timescaledb:2.13.1-pg16` **does not include** pgbackrest — which is exactly why this directory previously only delivered a "config contract". This round adds it via `Dockerfile.timescaledb`, making the solution usable.

## ⚠️ Correction: there is no official `pgbackrest/pgbackrest` image

The `pgbackrest/pgbackrest` recorded in `external/SOURCES.md:325` **does not exist**. Measured on 2026-10-11:

```
GET https://hub.docker.com/v2/repositories/pgbackrest/pgbackrest/tags
→ {"message":"object not found"}
```

Under `pgbackrest/*` there are only the `test` / `doc` repos. So this solution builds its own image following the officially recommended PGDG package repo (`external/pgbackrest/doc/xml/user-guide.xml:586`).

## Quick start

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

## How to run verification (two levels, don't mix)

```bash
# ① 静态：文件形态对不对（无需 Docker，秒级）
bash infra/ops/pgbackrest/verify-drs.sh

# ② 运行时：备份到底可不可用（需要真实仓库，**有真断言**）
#    在 pgbackrest 仓库边车内执行：
/opt/dr/verify.sh              # L1 校验和 + L2 新鲜度
/opt/dr/verify.sh --scratch    # 追加 L3：真恢复到临时目录并比对行数/时间戳
```

① only guarantees "the config won't fail to start due to syntax problems"; **backup usability is judged only by ②**.

## Status (Order 31 update)

- ✅ config, the three T2 entry scripts, compose, RUNBOOK, and T3 alert wiring are all in place;
- ✅ static self-check + metric-wiring self-check pass: `verify-drs.sh` / `infra/observability/verify_wiring.sh`;
- ✅ **real backup/recovery/verification has been actually run** (local PG16 + pgBackRest 2.59.3, official PGDG package): full backup → `verify.sh` L1+L2+L3 all green (incl. post-restore row-count/timestamp comparison) → after tampering a file `verify.sh` correctly turns red (negative case). Raw output in `docs/DR-RUNBOOK.md §6.2`;
- ⚠️ **the container orchestration layer has not yet been actually run** (containers cannot be started in this working environment): image build, three-layer merged stack-up, two-end TLS handshake, and CAgg recovery are unverified; see `docs/DR-RUNBOOK.md §6.3`.
