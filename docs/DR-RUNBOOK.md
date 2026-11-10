# Disaster Recovery Runbook (DR RUNBOOK) — TimescaleDB

> Order **25 / I-19** established the config contract; Order **31 / G-5** closed it out as "actually backing up".
> Related: `external/INTEGRATION-PLAN.md §8.9` (gap = audit #28).
> Target: the `timescaledb` service in `infra/docker-compose.yml` (DB objects in `infra/init.sql`).
> Tool selection: **pgBackRest** (selection rationale in §1).

**Key change in Order 31 (read this first)**: Order 25 only delivered the config and script **contract**; the image layer was still bare postgres, and the repo held no real backup. This order fills the gap:

| Item | Order 25 | After Order 31 |
|---|---|---|
| Backup entrypoint | `run-backup.sh` (full/diff/check/info) | **`backup.sh`** (+ incr / stanza-create, non-zero exit semantics explicit) |
| Restore entrypoint | `restore.sh` | `restore.sh` (kept) |
| Verify entrypoint | `verify-drs.sh` (**static**: YAML/syntax) | **`verify.sh`** (L1 checksum / L2 freshness / L3 restore comparison) + `verify-restore.sh` |
| Backup-failure alert | None | `monitor.sql` + postgres-exporter custom query + `BackupStale/BackupErrored` rules (reusing the #59 channel) |
| End-to-end evidence | None (no Docker in env) | **Real PG16 + pgBackRest 2.59.3 run**, see §6 |

---

## 0. One-Sentence Summary

100 robots × 10Hz = **1000 rows/s ≈ 8.64M rows/day** (`infra/init.sql` §6 comment, verified measurement basis).
This data "cannot be regenerated if lost" — the simulator only replays waveforms; once a historical curve is zeroed it is permanently gone.
This runbook provides: **how to run backups, which recovery path to use at what damage level, how much data can be lost (RPO), and how long the outage lasts (RTO)**.

---

## 1. Tool Selection: pgBackRest over wal-g

| Dimension | pgBackRest | wal-g | This Decision |
|---|---|---|---|
| Official Docker image | **None** (see "Correction" below) | Has `wal-g/wal-g` | Tie (both need image sourcing) |
| Incremental/block-incremental | ✅ `--type=diff/incr` + `repo-block` (v2.46+) | ❌ full + WAL only | **pgBackRest wins** |
| Repository encryption | ✅ `repo1-cipher-type=aes-256-cbc` (client-side) | Depends on external KMS | **pgBackRest wins** |
| Parallel/async archiving | ✅ `process-max` / `archive-async` | Limited | **pgBackRest wins** |
| Recovery consistency check | ✅ `check` + `verify` | Depends on external scripts | **pgBackRest wins** |
| Cloud object storage | ✅ repo1-type=s3/azure/gcs | ✅ native cloud advantage | wal-g slightly better |

**Conclusion**: This project's data is "single-node PG + needs minute-level RPO + needs incrementals", and pgBackRest's incrementals and built-in encryption fit better.
wal-g's advantage is concentrated in "native cloud object storage"; when there is a real S3 repo need it can coexist as repo2 (pgBackRest supports multiple repos).

### ⚠️ Correction of an Old Record (Fact Check)

`external/SOURCES.md:325` recorded the image as `pgbackrest/pgbackrest` —— **that image does not exist**. Verified on Docker Hub on 2026-10-11:

- `https://hub.docker.com/v2/repositories/pgbackrest/pgbackrest/tags` → `{"message":"object not found"}`
- Under `pgbackrest/*` there are actually only two repos: `pgbackrest/test` and `pgbackrest/doc`

Therefore this plan **builds its own image** (`infra/ops/pgbackrest/Dockerfile`), installing via the only officially recommended PGDG package repo:

- Verified `external/pgbackrest/doc/xml/user-guide.xml:586`:
  > "Ubuntu packages for pgBackRest are available at [apt.postgresql.org]"
- Installation method verified in the same file `:343-344`: first `apt-get install postgresql-common`, then run
  `/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y`.

Version pinned to **v2.59.3** (`external/pgbackrest/README.md:7`: "is the current stable release").

---

## 2. Architecture and T1 Final Plan (Order 31)

### 2.1 T1 Either/Or: Why the Final Answer Is "Both Derived Image + Repository Sidecar"

Order 31 §T1 asked to choose between (a) swapping to a postgres image with built-in pgbackrest and (b) official image + pgbackrest sidecar. **Conclusion: (a) and (b) are not alternatives, but two required halves of a two-end architecture**, both must be present — this is not "choosing both", it is dictated by pgBackRest's architecture:

- **WAL archiving runs locally on the DB host**: `archive_command = 'pgbackrest … archive-push %p'` is a command PostgreSQL's own process forks and executes, and **cannot** be replaced by a repo sidecar (the sidecar is not in PG's process space). ⇒ **The DB side must have the pgbackrest binary** ⇒ needs (a) the derived image.
- **The repository (backup destination) is a separate host**: backup data, encryption, and retention all live on the repo side, so a dedicated container must hold the `pgbackrest-repo` volume ⇒ needs (b) the sidecar.
- And the two ends' **versions must be exactly identical**: user-guide.xml:2770
  > "The pgBackRest version installed on the repository host must exactly match …"

So this plan = `Dockerfile.timescaledb` (add pgbackrest on top of the timescaledb image) **and** `Dockerfile` + `pgbackrest` sidecar. Both pin **v2.59.3** (verified as current stable in §1).

**Why not "pure (b) sidecar + mount PGDATA into the sidecar"**: that path requires the sidecar to do physical read/write on PG's data directory while also having PG archive on its own — two containers contending for the same PGDATA. The official docs never support this layout, and it conflicts with PG's checkpoint semantics. Rejected.

### 2.2 Why "Two-End"

pgBackRest is a **two-end architecture**: the PostgreSQL host and the repository host **each** must have a **completely identical version** of the pgbackrest binary and configuration.

> Verified `external/pgbackrest/doc/xml/user-guide.xml:2770`:
> "The pgBackRest version installed on the repository host must **exactly match**
> the version installed on the PostgreSQL host."

And PG's WAL archiving is a locally-executed `archive_command` (``pgbackrest ... archive-push %p``), so **the timescaledb container must contain the pgbackrest binary**. The base image `timescale/timescaledb:2.13.1-pg16` **does not contain** it, therefore:

```
┌─────────────────────────┐        ┌──────────────────────────┐
│ timescaledb (derived img)│        │ pgbackrest (repo sidecar) │
│  timescale/...:2.13.1   │        │  robot-monitor-pgbackrest │
│  + pgbackrest 2.59.3    │        │  pgbackrest 2.59.3        │
│                         │        │                           │
│  archive_command ───────┼──WAL──▶│  repo1-path=/var/lib/...  │
│  (archive-push %p)      │ archiv │  repo1-cipher=aes-256-cbc │
│                         │        │                           │
│  pg1-path=/var/lib/...  │◀─full──│  backup --type=full/diff  │
└─────────────────────────┘ backup └──────────────────────────┘
         ▲                                      │
         │       pgbackrest-repo volume (shared) │
         └──────────────────────────────────────┘
```

- DB-side config: `infra/ops/pgbackrest/pgbackrest-db.conf` (**does not contain** the encryption passphrase —— encryption happens on the repo side)
- Repo-side config: `infra/ops/pgbackrest/pgbackrest.conf` (contains `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}`)
- Container orchestration: `infra/ops/pgbackrest/docker-compose.dr.yml` (overlay)
- PG-side switch: `infra/ops/pgbackrest/postgresql-dr.conf` (`archive_mode=on` etc., loaded via `include_dir`)

---

## 3. RPO / RTO Targets (with Calculation, Not Guesswork)

### 3.1 Base Parameters (from this repo's verification)

| Parameter | Value | Source |
|---|---|---|
| Write rate | 1000 rows/s | `infra/init.sql` §6 comment: 100 robots × 10Hz |
| Row width | ≈1.34 KB/row | same (per-column width + 24B row header + 1.3x page overhead) |
| Raw data volume | ≈110 GiB/day | computed: 1000×86400×1.34KB |
| WAL generation | ≈1x data volume | conservative PG estimate (telemetry is continuous small-txn writes) |
| Retention policy | telemetry 30 days / alerts 180 days | `infra/init.sql` §6.2/§6.3 |
| Backup compression | zst ≈3x (conservative) | pgBackRest `compress-type=zst` (user-guide.xml:1477 recommends zst) |

### 3.2 RPO (how much data can be lost)

RPO is determined by the time for a WAL segment to be cut and successfully pushed to the repo:

- WAL segment size defaults to 16 MiB; at 1.34 MB/s write, **a segment fills in ~12 seconds** triggering `archive_command`;
- During night low-tide with no writes → segment not cut → `archive_timeout=30s` (`postgresql-dr.conf`) force-cuts as a fallback;
- plus network push latency and the `archive-push-queue-max=4GiB` buffer (local pile-up when repo briefly unavailable, no loss).

> **RPO target = 1 minute** (conservative; normal expected ≈15~30 seconds).
> Basis: segment fill ≈12s + push latency, capped by `archive_timeout=30s` during low-tide.

**Precondition**: the repo is available. If the repo is unavailable beyond the queue capacity of 4GiB, pgBackRest will **discard** that WAL segment and only emit a warning (verified `external/pgbackrest/doc/xml/reference.xml:1699`), the archive stream breaks → RPO invalidated, full backup must be redone.

### 3.3 RTO (how long the outage lasts)

RTO is not a single number; **take the worst of the two paths as the commitment**:

**Path A — data directory lost, rebuild entire DB from repo**
The repo holds 30 days of full data ≈ 1104 GiB (110GiB/day ×30 ÷3 compression):

| Repo Read Bandwidth | Restore Time |
|---|---|
| 100 MB/s (HDD/cross-net) | ≈188 min |
| 500 MB/s (local NVMe) | ≈38 min |
| 1000 MB/s (NVMe RAID) | ≈19 min |

**Path B — data directory still present (logical corruption, e.g. dropped table), `--delta` only patches differing blocks**
Last 7 days chunk diff ≈ 258 GiB:

| Repo Read Bandwidth | Restore Time |
|---|---|
| 100 MB/s | ≈44 min |
| 500 MB/s | ≈9 min |

> **RTO targets:**
> - **Normal target (Path B, local NVMe) ≤ 20 minutes** (`--delta` restore + WAL replay + promote)
> - **Extreme target (Path A, full rebuild) ≤ 4 hours** (100 MB/s floor; must be revised after measuring repo bandwidth)

**Honest disclosure**: The above are **capacity-based arithmetic estimates**, **not timed in a real environment** (no Docker in this work env, see §6). The first numbers must be replaced by a real recovery drill.

---

## 4. Recovery Steps

> Commands all run in the **pgbackrest repo sidecar** as the execution environment. Variable: `STANZA=robot_monitor`.

### 4.1 Phase Zero: Damage Assessment (decide Path A or B)

```bash
# 1) Can the cluster still start?
docker compose -f infra/docker-compose.yml exec timescaledb pg_isready -U postgres -d robot_monitor
# 2) If reachable, check key objects are intact
docker compose -f infra/docker-compose.yml exec timescaledb \
  psql -U postgres -d robot_monitor -c "SELECT count(*) FROM robot_metadata;"   # expect 100
# 3) Are the data directory physical files still present?
docker compose -f infra/docker-compose.yml exec timescaledb ls /var/lib/postgresql/data/PG_VERSION
```

- Files present, logical corruption only → **Path B** (§4.3)
- Data directory wholly lost/corrupted → **Path A** (§4.2)

### 4.2 Path A: Full DB Restore (latest backup)

```bash
# ① stop app + stop DB to avoid writes during restore
docker compose -f infra/docker-compose.yml stop backend alert-engine timescaledb

# ② see what's in the repo (no restore, list only)
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec pgbackrest pgbackrest --stanza=robot_monitor info

# ③ restore (script has built-in two-level confirmation; DR_FORCE_RESTORE=yes is the automation switch)
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec -e DR_FORCE_RESTORE=yes pgbackrest /opt/dr/restore.sh latest

# ④ start DB, verify
docker compose -f infra/docker-compose.yml start timescaledb
docker compose -f infra/docker-compose.yml exec timescaledb pg_isready -U postgres -d robot_monitor
```

Restore action semantics verified: `restore` defaults to the latest backup and automatically writes the required `restore_command` (`external/pgbackrest/doc/xml/reference.xml:2601`).

### 4.3 Path B: Point-in-Time Recovery (PITR, accidental drop/change)

```bash
# ① determine rollback target time (must include timezone; verified user-guide.xml:2248 "include timezone offsets")
#    target time should be before the fault operation by a second or two, to avoid boundary hit
TARGET='2026-10-11 08:30:00+08'

# ② stop app and DB (same as §4.2 ①), then run PITR
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec -e DR_FORCE_RESTORE=yes pgbackrest /opt/dr/restore.sh pitr "$TARGET"
```

The semantics and feasibility of `--type=time --target=<ts> --target-action=promote` verified:
`reference.xml:2601` restore section allow-list (`time` entry) + `user-guide.xml:2314` PITR example.

### 4.4 Post-Recovery Consistency Check (mandatory, do not skip)

> Starting with Order 31, this step is **automated**: `verify.sh --scratch` / `verify-restore.sh` will truly restore to a temp dir, start a read-only instance, and compare row counts / latest timestamps (see §4.6). The SQL below is the equivalent manual approach for troubleshooting.

```sql
-- 1) base objects
SELECT count(*) FROM robot_metadata;                      -- expect 100
-- 2) is recent data continuous (no large gaps)
SELECT max(time), min(time) FROM robot_telemetry WHERE time > now() - interval '1 hour';
-- 3) are continuous aggregate views still refreshing
SELECT * FROM timescaledb_information.jobs WHERE proc_name LIKE 'policy%';
-- 4) alert table schema not reverted (Order 19's primary key)
SELECT indexdef FROM pg_indexes WHERE tablename='robot_alert' AND indexname LIKE '%pkey%';
--     expect PRIMARY KEY (time, alert_id)
```

### 4.5 Backup Triggers (daily, Order 31 §T2 three entrypoints)

Under `/opt/dr/` there are three entrypoint scripts (**all idempotent, all exit non-zero on failure**):

| Script | Purpose | Key Exit Codes |
|---|---|---|
| `backup.sh` | `full` / `diff` / `incr` / `check` / `info` / `stanza-create` | 0 success; 2 usage error; 3 preconditions missing; rest pass-through pgbackrest |
| `restore.sh` | `latest` / `set <name>` / `pitr '<ts>'` | needs `DR_FORCE_RESTORE=yes` to act (two-level confirmation) |
| `verify.sh` | L1 checksum + L2 freshness (+ L3 restore comparison when `--scratch`) | 0 pass; 1 assertion failure; 2 usage error; 3 preconditions missing |

```bash
# 0) new env: create stanza first (idempotent)
... exec pgbackrest /opt/dr/backup.sh stanza-create

# 1) config/archive link check (must run this first after changing config)
... exec pgbackrest /opt/dr/backup.sh check

# 2) full (suggested every Sunday 06:30)
... exec pgbackrest /opt/dr/backup.sh full

# 3) diff (suggested Mon~Sat 06:30)
... exec pgbackrest /opt/dr/backup.sh diff

# 4) self-prove after backup: only verify.sh passing means "this backup is usable"
... exec pgbackrest /opt/dr/verify.sh
#    when true restore comparison is needed (stronger, longer):
... exec pgbackrest /opt/dr/verify.sh --scratch
```

**Scheduling suggestion** (cron semantics, verified `external/pgbackrest/doc/xml/user-guide.xml:1515-1520` crontab section):

```cron
#  m   h  dom mon dow  command
   30  06   *   *   0   /opt/dr/backup.sh full
   30  06   *   *   1-6  /opt/dr/backup.sh diff
   00  07   *   *   *   /opt/dr/verify.sh                 # daily freshness check
   00  03   *   *   0   /opt/dr/verify.sh --scratch       # weekly true restore drill
```

Container environments can use cron / systemd timer / orchestrator scheduled tasks.
**Key constraint**: full-backup period ≤ half the retention window (§4.7 retention policy).

### 4.6 Backup Authenticity Verification (Order 31 §T2: `verify.sh` three-layer assertions)

`verify.sh` **does not just echo OK**; it judges layer by layer with falsifiable assertions:

- **L1 repository consistency**: in the `pgbackrest verify --verbose` output
  - stanza section must have `status: ok`
  - every backup must have `status: valid`
  - must not appear `invalid checksum` / `missing: N>0` / `size invalid: N>0` / `other: N>0`
  > ⚠️ **Verified trap (2.59.3)**: when `verify` detects a checksum mismatch, **the process exit code is still 0**!
  > Judging only the exit code = false green. Therefore this layer parses the output text (see script comment).
- **L2 backup freshness**: parse `info --output=json` (official stable interface), assert
  - `status.code == 0`
  - at least 1 full backup
  - latest backup `error == false`
  - `now - timestamp.stop <= threshold` (default 86400s, tunable via `--max-age-seconds`)
  - WAL archive interval non-empty (`archive[-1].min/max` both present)
- **L3 restore authenticity** (`--scratch`): truly restore to a temp dir → start read-only instance → **compare `count(*)` and `max(time)` of source DB vs restored DB** (default tables: robot_telemetry / robot_metadata / robot_alert). When the environment does not support it, print a clear skip reason and exit 0.

```bash
# add threshold: fail if no new backup for over 2h
... exec pgbackrest bash -c 'PGBACKREST_MAX_BACKUP_AGE_SECONDS=7200 /opt/dr/verify.sh'
```

### 4.7 Retention Policy (consistent with `pgbackrest.conf`)

| Item | Value | Config Key |
|---|---|---|
| Full retention | 2 copies (count semantics) | `repo1-retention-full=2` |
| Diff retention | 6 copies | `repo1-retention-diff=6` |
| Repository encryption | aes-256-cbc | `repo1-cipher-type` (§5) |
| WAL archive queue ceiling | 4GiB (local pile-up when repo unavailable, no loss) | `archive-push-queue-max` |

> count semantics verified `reference.xml:944` (`repo-retention-full-type`): with `count`, "only surplus full backups are expired"; with `time`, it is by day. Default `count`.
> Constraint: full-backup period ≤ half the retention window — keeping 2 full backups ⇒ full interval ≤ 1 week (consistent with §4.5 cron).

---

## 5. Credential Management (no plaintext)

| Item | Storage | Mechanism |
|---|---|---|
| Repository encryption passphrase `repo1-cipher-pass` | **host `.env`** `PGBACKREST_CIPHER_PASS` | Injected into the container as same-named `PGBACKREST_*` variables via compose `environment`, then read by pgbackrest per official rules |
| Passphrase generation | `openssl rand -base64 48` | Official recommendation (from `user-guide.xml`'s configure-encryption section) |

Env var mapping rule verified (`reference.xml:2127`):

> "Any option may be set in an environment variable using the `PGBACKREST_` prefix
> and the option name in all caps replacing `-` with `_`"

I.e. `repo1-cipher-pass` → `PGBACKREST_REPO1_CIPHER_PASS`;
this plan deliberately uses `PGBACKREST_CIPHER_PASS` **so that the config file's `${PGBACKREST_CIPHER_PASS}` is interpolated**,
which ultimately still maps to the `repo1-cipher-pass` semantics (explicitly written in config as `repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}`).

**Guardrails**:
- `.env` is excluded by both `.gitignore` and `.dockerignore` (`AGENTS.md §5`);
- compose uses `${PGBACKREST_CIPHER_PASS:?…}` —— when unset, **compose errors out and exits directly**, rather than starting an empty-key repo;
- **no file in the repo contains a plaintext key**;
- DB-side config `pgbackrest-db.conf` **does not hold** the encryption passphrase (encryption only happens on the repo side).

### 5.1 Production Hardening: Two-End TLS Certificates (prerequisite for compose mount)

Both `pgbackrest-db.conf` and `pgbackrest.conf` use two-end TLS authentication (avoids SSH key distribution between containers); the cert volume `pgbackrest-certs` must be **generated first**, otherwise pgBackRest inside the container fails to read the certs.

```bash
# one-time generation of self-signed CA + two-end certs (CN must match tls-server-auth allowlist)
CERTDIR=./pgbackrest-certs   # or the actual mount point of compose volume pgbackrest-certs
mkdir -p "$CERTDIR"; cd "$CERTDIR"

# CA
openssl req -new -x509 -days 3650 -nodes -subj "/CN=pgbackrest-ca"   -keyout ca.key -out ca.crt

# repo-side server cert (tls-server-cert-file)
openssl req -new -nodes -subj "/CN=pgbackrest-client" -keyout server.key -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial   -days 3650 -out server.crt

# DB-side server cert (pgbackrest-db.conf's tls-server-cert-file)
openssl req -new -nodes -subj "/CN=pgbackrest-client" -keyout client.key -out client.csr
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial   -days 3650 -out client.crt

chmod 600 *.key
```

> Certificate generation follows the official guide: `external/pgbackrest/doc/xml/user-guide.xml:2846`
> "pgBackRest expects client/server certificates to be generated in the same way as
> PostgreSQL" (i.e. use openssl to generate a self-signed CA+cert).
>
> **Note**: Officially authorization is by CN (`tls-server-auth=<CN>=<stanza>`, user-guide.xml:1859).
> This project's configure uses `pgbackrest-client` as the agreed CN in stdin;
> in real deployment please make **the cert CN match the allowlist in the config**, otherwise the handshake is rejected.

---

### 5.2 Backup-Failure Alert Wiring (Order 31 §T3, reusing the existing #59 channel)

**Do not build a separate alert channel**: `#59` already turned "alert persistence failure" into
`robot_alert_store_errors_total` → Prometheus rule `AlertStoreFailing` →
Alertmanager (`infra/observability/alertmanager.yml`) → webhook.
Backup failure reuses the same chain, only adding a **metric source**:

```
pgbackrest --output=json info         (official stable interface)
        │  COPY FROM PROGRAM (official user-guide.xml Monitoring example)
        ▼
monitor.pgbackrest_info()  →  view monitor.pgbackrest_backup_status
        │  infra/ops/pgbackrest/monitor.sql
        ▼
postgres-exporter --extend.query-path=postgres-exporter-queries.yaml
        ▼
pgbackrest_backup_last_success_timestamp_seconds / _last_errored / pgbackrest_stanza_status_code
        ▼
infra/observability/rules/robot-alerts.yml: BackupStale / BackupErrored / BackupStanzaUnhealthy
        ▼
Alertmanager → robot-monitor-webhook (same chain as AlertStoreFailing)
```

Three rollout steps:

```bash
# ① create function and view on DB side (once; see monitor.sql, official example pattern)
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml \
  exec timescaledb psql -U postgres -d robot_monitor -f /opt/dr/monitor.sql

# ② confirm the view can read repo status (returns one row: status_code=0 = healthy)
... exec timescaledb psql -U postgres -d robot_monitor \
  -tAc 'SELECT * FROM monitor.pgbackrest_backup_status'

# ③ attach the custom query to postgres-exporter (observability overlay already configured)
docker compose -f infra/docker-compose.yml -f infra/observability/docker-compose.observability.yml up -d
curl -s localhost:9187/metrics | grep pgbackrest_
```

Semantic boundaries of the three rules:

- `BackupStale`: `time() - pgbackrest_backup_last_success_timestamp_seconds > 93600` (26h).
  A **periodic** event; the criterion is "freshness", not instantaneous `> 0`.
- `BackupErrored`: `pgbackrest_backup_last_errored > 0`. The latest backup is incomplete
  (≠ no backup, but "the latest one is not usable for recovery").
- `BackupStanzaUnhealthy`: `pgbackrest_stanza_status_code > 0`.
  Stanza path lost/config error —— at which point `backup.sh` itself would already fail.

> ⚠️ Upstream status: `--extend.query-path` is marked DEPRECATED in postgres_exporter
> ("Adding new metrics via a config file (DEPRECATED)" section), still supported in v0.17.1.
> If removed in the future, the only migration point is one place (the view `monitor.pgbackrest_backup_status` stays unchanged).

---

## 6. Verification Status (Order 31 update: from "unverified" to "with real evidence")

### 6.1 Environment and Version (verified)

Order 31's acceptance was completed on a **real local PG cluster** (not mock): using the official PGDG repo
(same install path as `Dockerfile`) installed `postgresql-16` + `pgbackrest`:

```
$ pgbackrest version
pgBackRest 2.59.3          # matches the version pinned in pgbackrest.conf / Dockerfile

$ /usr/lib/postgresql/16/bin/postgres --version
postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2)
```

> Why this counts as "real" verification: `pgbackrest` is the official package, version exactly 2.59.3 pinned in the repo;
> backup/recovery/verification go through the **same pgBackRest code path** (§4 / §5 commands executed verbatim),
> only "container" is swapped for "local process". The **container orchestration layer** has separate unverified items (§6.4).

### 6.2 Completed and Passed Runs (with original output)

**① Real full backup + verification** (`backup.sh`):

```
$ backup.sh full
INFO: backup start archive = 000000010000000000000002, lsn = 0/2000028
INFO: new backup label = 20261013-000357F
INFO: full backup size = 32.6MB, file total = 1379
INFO: backup command end: completed successfully (4601ms)

$ backup.sh check      # triggers pg_switch_wal and confirms required WAL has reached repo
… completed successfully
```

**② Three-layer authenticity verification** (`verify.sh --scratch`, exit code 0):

```
== L1 repository consistency (pgbackrest verify: per-file checksum) ==
L1a OK: verify reports stanza status: ok
L1b OK: verify reports each backup status: valid (4 total)
L1c OK: no invalid/missing/checksum/other anomalies
== L2 backup freshness and integrity (assert info JSON) ==
L2a OK: stanza status.code=0 (ok)
L2b OK: repo has 2 backups total, of which 2 full (retention baseline exists)
L2c OK: latest backup 20261013-000617F has no error (error=false)
L2d OK: latest successful backup is 359s ago ≤ threshold 86400s
L2e OK: WAL archive interval non-empty [000000010000000000000006 .. 00000001000000000000000C]
== L3 restore authenticity (true restore to temp dir + row-count/timestamp comparison) ==
baseline robot_telemetry: count=5000 max_time=2026-10-13 00:03:54.696285+08
baseline robot_metadata: count=100
restore artifact check: PG_VERSION=16, file count=1381
OK table robot_telemetry row count matches: 5000
OK table robot_telemetry max(time) matches: 2026-10-13 00:03:54.696285+08
OK table robot_metadata row count matches: 100
restored DB timescaledb=2.30.2, chunk count=1
all restore authenticity passed: source DB and restored DB row counts / latest timestamps match.
all assertions passed (L1 + L2 + L3).        # exit code 0
```

**③ Negative case: assertions really fail** (otherwise it is false green). Tamper one byte block of one backup file in the repo:

```
$ verify.sh
L1a FAIL: verify did not report stanza status: ok
L1c FAIL: verify detected invalid file -- repo corrupted, backup not usable for recovery
unpassed assertions present.                        # exit code 1

# after restoring the tampered file
$ verify.sh … exit code 0 (back to all green)
```

> This negative case also exposes a **real trap**: when `pgbackrest verify` detects
> an `invalid checksum`, **the process exit code is still 0**. Therefore `verify.sh`'s L1 parses the output text,
> rather than judging only the exit code —— an implementation that judges only the exit code would report false green when the repo is corrupted.

**④ Backup status observable** (`monitor.sql` → postgres-exporter → rules):

```
$ psql -d robot_monitor -tAc 'SELECT * FROM monitor.pgbackrest_backup_status'
robot_monitor|0|1791821181|f|00000001000000000000000C
                │        └ last_backup_stop (epoch 1791821181 = 2026-10-13 00:06:21)
                └ status_code=0 (ok)
```

Prometheus rules validated with official `promtool` (version v3.2.1, consistent with compose pin):

```
$ promtool check rules infra/observability/rules/robot-alerts.yml
SUCCESS: 12 rules found      # includes the new BackupStale / BackupErrored / BackupStanzaUnhealthy
```

**⑤ Static and gating checks** (reproducible):

```bash
bash infra/ops/pgbackrest/verify-drs.sh          # file shape: YAML/structure/no plaintext key/script syntax
bash infra/observability/verify_wiring.sh        # backup metric wiring: rule ref ↔ source ↔ exporter mount
docker compose -f infra/docker-compose.yml -f infra/ops/pgbackrest/docker-compose.dr.yml config
docker compose -f infra/ops/pgbackrest/docker-compose.dr.yml config
```

The first three all pass (compose config exit code 0; see Appendix A for full output). `verify-drs.sh`
**only proves "config won't fail to start due to syntax issues"**; backup usability is judged by `verify.sh` —— the division of labor is noted in the script header.

### 6.3 Unverified List (honest boundary, do not delete)

The following were **not** actually run in this environment, because they depend on "real container orchestration layer" or "real disk bandwidth":

| # | Unverified item | Required condition | Verification method |
|---|---|---|---|
| U1 | Can `Dockerfile.timescaledb` (Alpine source build of pgbackrest) be built | A machine that can run Docker build | `docker build -f Dockerfile.timescaledb`; build has built-in `pgbackrest version` self-check |
| U2 | Can `Dockerfile` (PGDG install) be built | Network access to apt.postgresql.org | `docker build -f Dockerfile .`; build has built-in `pgbackrest version` + `jq --version` self-check |
| U3 | Can the three-layer merged overlay actually bring up the stack | Docker Compose v2 + available kernel | `docker compose … up -d` (locally config is already judged parseable, see Appendix A) |
| U4 | `include_dir` accepted by timescaledb's PG | Runtime | `SHOW include_dir;` / `SHOW archive_mode;` |
| U5 | Two-end TLS cert handshake | Runtime | Generate certs (§5.1) then run `backup.sh check` |
| U6 | **RTO real timed measurement** | Real disk + generate data | Walk §4.2/§4.3 once and time it, replace §3.3 estimates with real values |
| U7 | Repo read bandwidth real measurement | Real disk | `pgbackrest … restore` timing |
| U8 | TimescaleDB continuous aggregate (CAgg) recovery under physical backup | Needs community-edition timescaledb | Local verification used Apache edition (CAgg unavailable, so `init.sql`'s CAgg section not covered); production image is community edition, needs in-container recheck |

> **No drill records were fabricated** (Order 31 forbidden item / Order 25 §4 forbidden item 2).
> The local-run evidence (§6.2) is **real commands with real output**; the container-layer gaps are listed separately in §6.3.

### 6.4 Difference Between Local Run and Container Environment (must know)

| Dimension | Local verification | Production (compose) |
|---|---|---|
| pgbackrest version | 2.59.3 (PGDG package) | 2.59.3 (PGDG package / Alpine source build), **consistent** |
| PG version | 16.15 | 16 (timescaledb 2.13.1-pg16 ships 16.1), **major version consistent** |
| timescaledb extension | 2.30.2 (Apache edition, PGDG) | 2.13.1 (community edition, image-built), **different version** |
| `postgresql.conf` location | Debian split-config (`/etc/postgresql/16/main/`) | Inside PGDATA (`/var/lib/postgresql/data/`) |
| Two-end auth | Local Unix socket | TLS two-end certs (`pg1-host-type=tls`) |

**Conclusion**: backup/recovery **code path** is verified; **container orchestration + TLS + CAgg** are unverified items in §6.3.
The timescaledb extension version difference does not affect the correctness of the physical backup itself (it backs up files and WAL),
but the CAgg-related recovery assertion needs recheck in the production image (U8).

---

## Appendix A: Reproducible Judgment Commands and Original Output (Order 31 acceptance)

### A.1 compose config (T1 judgeable criterion)

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
... (exit code 0; full 200+ lines omitted, can be reproduced locally)
```

Parse the DR overlay file alone (the literal command of Order 31 acceptance criterion #1):

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
# exit code 0
```

### A.2 T2 three entrypoints --help / dry-run (skip semantics when no real DB)

```console
$ /opt/dr/backup.sh --help      # exit code 0
$ /opt/dr/backup.sh bogus       # exit code 2 (unknown mode, prints available values)
$ /opt/dr/backup.sh full        # exit code 3 (stanza does not exist, prints "run stanza-create first")
$ /opt/dr/verify.sh             # exit code 0 (L1+L2 pass) / 1 (assertion fail) / 3 (precondition missing)
$ /opt/dr/verify.sh --scratch   # adds L3 true restore comparison; prints skip reason and exits 0 when env unsupported
```

### A.3 Negative Case (proves assertions are not false green)

After tampering one backup file in the repo:

```console
$ verify.sh
L1a FAIL: verify did not report stanza status: ok
L1c FAIL: verify detected invalid file -- repo corrupted, backup not usable for recovery
# exit code 1
```
