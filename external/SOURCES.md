# External reference sources (download first, don't hand-write)

This directory holds **actually downloaded** third-party/official repos, used to cross-check implementations and avoid hand-writing from scratch.
The directory itself is in `.gitignore` (third-party source is not poured into the main repo), but **this file is committed**, so CNB NPC and human reviewers can see the sources and their comparison conclusions.

All downloads use `git clone --depth 1 <url>`, sourced from official GitHub org repos.

---

## 1. `mqtt-to-timescaledb` — EMQX official

- Source: `https://github.com/emqx/mqtt-to-timescaledb` (EMQX official org `emqx`)
- Download: `git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git`
- Size: 1.1 MB | Key files: `create-table.sql`, `docker-compose.yml`, `emqx/cluster.hocon`
- License: retained with the repo

### Comparison conclusion

| Item | Official approach | This repo | Verdict |
|---|---|---|---|
| EMQX image | `emqx/emqx-enterprise:5.0.3` (enterprise)<br>open-source edition is `emqx/emqx` | `emqx/emqx:5.6` | ✅ equivalent, kept |

### 1.1 ⚠️ Retraction: the earlier conclusion that "`emqx:5.6` is a non-EMQX image" is **wrong**

On 2026-10-10 we judged by digest difference that "the bare name `emqx:5.6` resolves to a non-EMQX image". **That conclusion is void**, because **the comparison method itself was wrong**, unrelated to the image:

**Error 1 — treating a "child manifest's digest" as the "image id", then comparing across repos**

```
library/emqx:5.6  index digest              = sha256:4cbb5792d0fd17e02f3003cda571aeca343d954f05215f8ab20a5a3b21603755
  └─ amd64 子 manifest digest               = sha256:06f2341d1bf4ec8350a37a029dc5f6590c2dc95f181bebf1493d0f8e3a727f8e
emqx/emqx:5.6     index digest              = sha256:b14a3c6d04f680869e3ca181b552c9338b7cd20c2d43d8af6c1e3b8aec48a0cc
```

What the docs wrote as "`emqx:5.6` → `06f2341d…`" is actually `library/emqx`'s **child manifest** digest, but it was compared against `emqx/emqx`'s **index** digest. Different granularity, so they inevitably "don't match".

**Error 2 — ignored the fact that "one image has two release channels"**

`library/emqx` **is** built from `emqx/emqx-docker` (Docker Official Images):

```
$ curl https://raw.githubusercontent.com/docker-library/official-images/master/library/emqx
  Maintainers: Rory Z (@rory-z), Ivan Dyachkov (@id)
  GitRepo: https://github.com/emqx/emqx-docker.git     ← same origin as emqx/emqx
  Tags: 5.7.2, 5.7      Directory: 5.7
  Tags: 5.8.8, 5.8, 5, latest   Directory: 5.8
```

The two channels' differing index digests are an **inevitable result of different build methods** (bashbrew rebuild vs official CI). The EMQX official README also recommends `emqx/emqx` and `emqx` (=library/emqx) side by side.

**Positive evidence: `library/emqx:5.6` is indeed EMQX 5.6.1**

```
$ curl -H "Authorization: Bearer $T"     https://registry-1.docker.io/v2/library/emqx/blobs/sha256:3a5ff702742d32ef92978cc8fe0122f1a127f136e7c006de0a807b0c7f7324b9
  config.Env        = ["...", "EMQX_VERSION=5.6.1", "AMD64_SHA256=a5be376…", ...]
  config.Entrypoint = ["/usr/bin/docker-entrypoint.sh"]
  config.Cmd        = ["/opt/emqx/bin/emqx", "foreground"]
  config.WorkingDir = "/opt/emqx"
  config.ExposedPorts = 1883 / 8883 / 18083 / 4370 / 5369 / 8083 / 8084
```

It matches the `emqx/emqx-docker` 5.6 branch `Dockerfile` (`ENV EMQX_VERSION=5.6.1`, same Entrypoint/Cmd, same EXPOSE list) **line by line**.

**So why does this repo still keep `emqx/emqx:5.6`?** (this judgment is justified, not mere inertia)

`library/emqx` currently **only maintains 5.7 / 5.8** (proven by the Tags section of the official-images file above). `library/emqx:5.6` is a legacy **unmaintained tag** on Docker Hub — it still exists but will no longer receive security updates, nor be re-published through the official-images process.
At the 5.6 tier, `emqx/emqx:5.6` is the continuously-published **traceable source**.

> Lesson: a digest can only tell whether "the bytes are identical", **not** whether "it's the same project". Before comparing digests across release channels, first confirm the two's **build source** is the same.

**Official approaches not adopted (intentional, not an oversight)**

**Official approaches not adopted (intentional, not an oversight)**

- The official `create_hypertable('energy_log','timestamp','factory_id',4)` 4th param is the **space-partition count**, aimed at wide tables where "devices are columns". This repo uses a narrow table (one row per robot per timestamp), so it does not use space partitions.
- The official example uses the **EMQX Rule Engine to write TimescaleDB directly**. That capability belongs to EMQX **Enterprise**; this repo is locked to the open-source edition (`emqx/emqx`), so it keeps the FastAPI(aiomqtt) relay route — the relay is necessary: Redis state cache, alert evaluation, and WS push all happen at this layer.
- The official example has no continuous aggregates and no primary key; this repo has both (CAgg for the dashboard, PK for dedup).
- The official compose declares `networks: emqx-net` at the top level but no service references it — official redundancy, not imitated.

---

## 2. `tsbs` — TimescaleDB official stress suite

- Source: `https://github.com/timescale/tsbs` (Timescale official org `timescale`, ★1451)
- Download: `git clone --depth 1 https://github.com/timescale/tsbs.git`
- Size: 3.0 MB
- Purpose: write-throughput baseline reference for **Order 06 (integration & stress)**. `cmd/` provides loaders like `tsbs_load_timescaledb`, and `pkg/targets/` defines time-series data models.
- Current status: downloaded, **not yet used**. When Order 06 is dispatched, the NPC should be asked to first read `tsbs/README.md` and the stress methodology under `docs/`, then decide whether to reuse its loader or stick with this repo's own `simulator/` (the latter produces 24-joint humanoid-robot load more precisely).

---

---

## 3. Second batch: full download by requirement point (2026-10-10)

**Adoption and integration plan: see `external/INTEGRATION-PLAN.md`** (this doc only records sources and trade-offs).

| Requirement | Repo | Size | stars | Adoption conclusion |
|---|---|---|---|---|
| Broker-layer stress | `emqx/emqtt-bench` | 350 KB | 568 | adopted by Order 06, complements `mock_fleet.py` (see I-4) |
| MQTT client contract | `empicano/aiomqtt` | 430 KB | 577 | contract check → I-1 upgrade, I-2 instance reuse |
| Frontend state management | `pmndrs/zustand` | 3.6 MB | 58801 | version check, **decided not to upgrade** (see I-3 decision table) |
| Async Redis usage | `redis/redis-py` | 18 MB | 13647 | version check, **decided not to upgrade** |
| Full-stack structure reference | `fastapi/full-stack-fastapi-template` | 3.5 MB | 45929 | structure comparison |
| Alert design standard | `prometheus/alertmanager` (sparse `docs/`) | 816 KB | 8642 | comparison found a missing "inhibition" mechanism (I-5) |
| Metric exposure | `prometheus/client_python` | 14 MB | 4380 | optional for Order 06 |

> ⚠️ The `aiomqtt` repo has moved from `sbtinstruments/aiomqtt` **to `empicano/aiomqtt`** (proven: `gh api repos/sbtinstruments/aiomqtt` returns `empicano/aiomqtt`). The PyPI package name `aiomqtt` is unchanged, so `requirements.txt` needs no package-name change.

### Same as above: searched but **not downloaded** (poor size/value ratio or already covered by the package manager)

| Repo | Size | Reason |
|---|---|---|
| `grafana/grafana` | **2011 MB** | size is fatal; this project builds its own dashboard; Grafana is an alternative, not a dependency |
| `apache/echarts` | 264 MB | already pulled in via npm; proven `frontend_dashboard/node_modules/echarts` exists |
| `pydantic/pydantic` | 458 MB | already pulled in via pip; 458 MB for type-annotation details isn't worth it |
| `timescale/timescaledb` | 65 MB | the bulk is C-extension source; this project only needs the SQL-layer contract (already covered by the official example) |
| `fastapi/fastapi` | 53 MB | already pulled in via pip; the 6 MB template already covers structure needs |

> **Principle**: dependencies already pulled in by a package manager (npm/pip) are **not cloned as source**. `clone` is only used to get "things not in the package" — deployment contracts, best-practice docs, stress tools.

---

## 4. Third batch: fill gaps in the data layer and frontend bridging (2026-10-10 full re-scan)

The re-scan found the **core data-layer dependency had no reference source at all** — `asyncpg` is the lifeblood of batch storage.

| Requirement | Repo | Size | stars | Adoption conclusion |
|---|---|---|---|---|
| **Async PG driver** | `MagicStack/asyncpg` | 2.0 MB | 8105 | **I-6 must upgrade 0.29.0 → 0.30+**, see below |
| Frontend chart bridging | `hustcc/echarts-for-react` | 292 KB | 5005 | direct dependency check |
| WS underlying implementation | `Kludex/starlette` | 7.0 MB | 12654 | WebSocket contract check |
| DB migration | `sqlalchemy/alembic` | 4.1 MB | 4438 | future migration-scheme evaluation |
| MQTT underlying client | `eclipse-paho/paho.mqtt.python` | 1.3 MB | 2429 | aiomqtt's lower-level dependency |

### ⭐ Key finding: `asyncpg` 0.29.0 is the **root cause** of "can't verify locally", not an environment limitation

| Version | Release date | Python support |
|---|---|---|
| `v0.29.0` (our pinned) | 2023-11-05 | `requires-python = '>=3.8.0'`, **no 3.13 classifier** |
| `v0.30.0` | 2024-10-20 | **"Support Python 3.13 and PostgreSQL 17"** |
| `v0.31.0` | 2025-11-24 | + Python 3.14 |
| `v0.32.0` (latest) | 2026-10-06 | + Python 3.15 |

The HEAD `pyproject.toml` classifiers empirically list `3.9/3.10/3.11/3.12/3.13/3.14`.

Previously we attributed "`asyncpg==0.29.0` fails to compile on Python 3.13 (`_PyLong_AsByteArray` signature change)" to an **environment error**, but it was actually **too-old a version**. The blocker disappears after upgrading to 0.30+.

### ⭐ Side finding: PyPI 403 can be bypassed with a mirror source

Locally `files.pythonhosted.org` returns 403, but the **Tsinghua mirror works**:

```bash
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple asyncpg
# measured: Python 3.13.14 + asyncpg 0.32.0 installed successfully and importable, 29s (had a wheel)
```

This means backend items previously marked "unverified (can't run locally)" **can now actually be run and verified**.

### Not downloaded in the third batch

| Repo | Size | Reason |
|---|---|---|
| `puppeteer/puppeteer` | **1533 MB** | size is fatal; already pulled in via npm (`frontend_dashboard/node_modules`) |
| `redis/redis` | 212 MB | we only use the client; `redis-py` already covers the need |

---

## Reproduction commands

```bash
mkdir -p external && cd external
git clone --depth 1 https://github.com/emqx/mqtt-to-timescaledb.git
git clone --depth 1 https://github.com/timescale/tsbs.git
git clone --depth 1 https://github.com/emqx/emqtt-bench.git
git clone --depth 1 --branch v2.5.1 https://github.com/empicano/aiomqtt.git
git clone --depth 1 https://github.com/pmndrs/zustand.git
git clone --depth 1 https://github.com/redis/redis-py.git
git clone --depth 1 https://github.com/fastapi/full-stack-fastapi-template.git
git clone --depth 1 https://github.com/prometheus/client_python.git
git clone --depth 1 --filter=blob:none --sparse https://github.com/prometheus/alertmanager.git
cd alertmanager && git sparse-checkout set docs && cd ..
git clone --depth 1 https://github.com/MagicStack/asyncpg.git
git clone --depth 1 https://github.com/hustcc/echarts-for-react.git
git clone --depth 1 https://github.com/Kludex/starlette.git
git clone --depth 1 https://github.com/sqlalchemy/alembic.git
git clone --depth 1 https://github.com/eclipse-paho/paho.mqtt.python.git
```

> Note: the `encode/uvicorn` repo has moved to **`Kludex/uvicorn`** (proven: API returns `Kludex/uvicorn`).

---

## Repos searched but **judged not worth downloading** (archived to avoid re-searching)

| Repo | stars | Reason not to download |
|---|---|---|
| `zmops/zeus-iot` | 729 | full IoT platform; size and dependencies far exceed this project's needs |
| `KouShenhai/KCloud-Platform-IoT` | 674 | same as above, Java stack, incompatible with this project's Python/TS |
| `dave-wind/awesome-python` | — | only a single `app.py` example; no schema/compose reference value |
| `innovaassolutions/MQTTtoMCP` | — | coupled to an OpenAI/GPT query layer, unrelated to this project |
| `robot fleet monitoring dashboard`-type (`Panca2022/Robot-Monitor-Dashboard` etc.) | 0–2 | toy projects, stars too low, no code-quality guarantee |

> Principle: better to download less than to import low-quality code as "reference". A reference source must be an **official org repo** or a high-quality repo proven by digest/stars.

---

## 5. Fourth batch: full download execution + version-drift evidence (2026-10-10 re-scan)

Per the §1–§4 list, **fully executed** `git clone --depth 1`, 14/14 all succeeded. Sizes and stars matched expectations, none failed. Actual list (`git rev-parse --short HEAD`):

| Repo | Size | HEAD | Remote |
|---|---|---|---|
| `emqx/mqtt-to-timescaledb` | 1.2 MB | `ee24974` | emqx/mqtt-to-timescaledb |
| `timescale/tsbs` | 2.9 MB | `8323e59` | timescale/tsbs |
| `emqx/emqtt-bench` | 388 KB | `cb86b18` | emqx/emqtt-bench |
| `empicano/aiomqtt` | 872 KB | `c8f8248` (v2.5.1) | empicano/aiomqtt |
| `pmndrs/zustand` | 3.6 MB | `d7a5583` | pmndrs/zustand |
| `redis/redis-py` | 18 MB | `22d47b5` | redis/redis-py |
| `fastapi/full-stack-fastapi-template` | 3.6 MB | `7257e60` | fastapi/full-stack-fastapi-template |
| `prometheus/alertmanager` (sparse docs) | 892 KB | `e7a4afd` | prometheus/alertmanager |
| `prometheus/client_python` | 14 MB | `9cd073c` | prometheus/client_python |
| `MagicStack/asyncpg` | 2.0 MB | `13b0191` | MagicStack/asyncpg |
| `hustcc/echarts-for-react` | 372 KB | `2346edc` | hustcc/echarts-for-react |
| `Kludex/starlette` | 7.1 MB | `96d5023` | Kludex/starlette |
| `sqlalchemy/alembic` | 4.1 MB | `1bc21b8` | sqlalchemy/alembic |
| `eclipse-paho/paho.mqtt.python` | 1.4 MB | `b48baee` | eclipse-paho/paho.mqtt.python |

### ⚠️ Empirical finding: `aiomqtt`'s `--depth 1` has drifted to v3.0.0-alpha.1

| Item | Fact | Evidence |
|---|---|---|
| HEAD pulled by bare `--depth 1` | **`3.0.0-alpha.1`** (v3 rewrite line) | `pyproject.toml` `version = "3.0.0-alpha.1"` |
| v3 breaking changes | deprecated paho-mqtt for `mqtt5`; `Client` params all keyword-only; `MqttError` replaced by `ConnectError` etc.; `reconnect=True` becomes built-in | `CHANGELOG.md` `## [3.0.0-alpha.1]` section |
| This project's pinned version | **`2.5.1`** (`backend_ingestion/requirements.txt`) | locked with `==` |
| I-1/I-2 evidence in `INTEGRATION-PLAN.md` | references `docs/reconnection.md` (v2.5.1 path) | a bare clone **can't find that file** |

**Handling**: explicitly checked out `external/aiomqtt` to the `v2.5.1` tag (`git fetch --depth 1 origin refs/tags/v2.5.1:refs/tags/v2.5.1 && git checkout v2.5.1`, HEAD becomes `c8f8248`). After switching back, both `docs/reconnection.md` and `docs/alongside-fastapi-and-co.md` **exist**, and I-2's quoted "reusable (but not reentrant)" text lands at `docs/reconnection.md:5` — consistent with the plan.

> Lesson: `--depth 1` only pins "today's HEAD", not the version. If a reference source is used to check a **pinned version**'s API, you must `checkout <tag>`; otherwise the next re-scan silently picks up the new mainline's breaking changes, invalidating file:line evidence.

### Key-file spot-checks for the remaining repos (all hit)

| Repo | Spot-checked file | Result |
|---|---|---|
| `mqtt-to-timescaledb` | `create-table.sql`, `docker-compose.yml`, `emqx/cluster.hocon` | ✅ exists |
| `tsbs` | `README.md`, `cmd/`, `docs/` | ✅ exists |
| `emqtt-bench` | `README.md` contains `--prometheus` | ✅ `README.md:101` |
| `alertmanager` | `docs/configuration.md` contains `inhibit_rules` | ✅ `:165-167` |
| `asyncpg` | `pyproject.toml` classifiers include 3.13/3.14 | ✅ `:26-27` |
| `starlette` | `starlette/websockets.py` | ✅ exists |
| `paho.mqtt.python` | `src/paho/mqtt/client.py` | ✅ exists |
| `echarts-for-react` / `zustand` / `redis-py` / `alembic` / `full-stack-fastapi-template` | `package.json` / `redis/asyncio/client.py` / `setup.cfg` / `README.md` | ✅ exists |

## 6. Fifth batch: re-scan from scratch (incl. 'full replacement' candidates) —— 2026-10-10

> ⚠️ **This section (§6) is an "evaluation candidate pool / research notes", not a "downloaded list".** The repos below **have not been cloned to this repo's disk** — the disk only contains the 22 sources from `fetch-sources.sh` (§5 + §7). They are alternative/supplementary options **evaluated** while tracing upstream for each of the 32 audit items. If you decide to adopt one in the future, you must first add it to the `SOURCES` array in `external/fetch-sources.sh` and actually clone it, then update this section accordingly.

> **This round changed the method.** The first four batches "guessed needs from names"; this round traces upstream item-by-item for every requirement point in the **32-item audit** that still lacked a reference source, and **actively seeks more perfect replacements** (not filling gaps, but asking "is the current pick the best").

> Judgment criteria: ① does the requirement point really exist (backed by `file:line` or an audit number) ② is the repo active (`pushed_at`) ③ is there an official release image / can it run directly (decides "download" vs "deploy a service") ④ is there a better alternative (can replace an existing dependency).

### 6.1 Candidate sources (under evaluation, not yet cloned to this repo)

> The table below is **evaluation candidates**, not "downloaded". This repo's disk only contains the 22 sources from `fetch-sources.sh` (§5 + §7). To actually adopt, you must first add to the `SOURCES` array in `fetch-sources.sh` and actually clone.

| # | Requirement point (corresponding audit/gap) | Repo | Size | HEAD | Key evidence |
|---|---|---|---|---|---|
| N1 | MQTT gap → OTel collection pipeline (#32 data freshness / unified telemetry) | `open-telemetry/opentelemetry-python` | 12 MB | `c4d8ff6` | `opentelemetry-sdk/` exists |
| N2 | frontend RUM/tracing (audit "frontend unobservable") | `open-telemetry/opentelemetry-js` | 22 MB | `e5db961` | `api/`, `e2e-tests/` |
| N3 | collector/edge forwarding (unified metrics+logs+trace) | `open-telemetry/opentelemetry-collector` | 22 MB | `e85aabc` | official collector |
| N4 | collector distribution (config-driven pipeline, with Docker) | `grafana/alloy` | 69 MB | `eabe529` | `collector/README.md`, `docs/` |
| N5 | **PG metrics** (direct fix for guardrail "no metrics export") | `prometheus-community/postgres_exporter` | 1.4 MB | `fb6be9d` | `README.md:252` `DATA_SOURCE_NAME` |
| N6 | **Redis metrics** (Redis is a single point, must be observable) | `oliver006/redis_exporter` | 1.2 MB | `6de1e8b` | `README.md:197` `REDIS_ADDR`, `:216` `/metrics` |
| N7 | **stress testing with writable assertions** (R-07/R-08 "judgeable") | `grafana/k6` | 75 MB | `b408672` | `go.mod` `go.k6.io/k6/v2` |
| N8 | k6 MQTT extension (fill "MQTT can be stressed") | `pmalhaire/xk6-mqtt` | 312 KB | — | 312 KB, `git ls-remote` 17 refs |
| N9 | **auth/RBAC** (audit "grep auth zero hits") | `fastapi-users/fastapi-users` | 1.2 MB | `e9c57ff` | `__init__.py` `15.0.5`; `pyproject.toml:133` `fastapi>=0.65.2` |
| N10 | enterprise SSO (reserved, not default) | `keycloak/keycloak` | 205 MB | `eb6d2dde` | Java, official image |
| N11 | lightweight SSO (cheaper for a single factory) | `casdoor/casdoor` | 38 MB | `43a6aab` | Go+Vue |
| N12 | **event/alert closed-loop** (ack + escalation + on-call) | `keephq/keep` | 133 MB | — | `providers/` includes `slack/teams/telegram/webhook/ntfy` |
| N13 | Alertmanager proper (inhibition/silence truly implemented) | `prometheus/alertmanager` | 7.1 MB | `e7a4afd` | `docs/configuration.md` `inhibit_rules` |
| N14 | alert → enterprise IM bridge (Python, trimmable) | `idealista/prom2teams` | 1.0 MB | `aaecfd5` | Teams webhook forwarding |
| N15 | status page / announcements (ops-facing) | `openstatusHQ/openstatus` | 218 MB | — | `docker-compose.yaml` |
| N16 | **Pydantic contract generation** (avoid manual 24-joint comparison) | `koxudaxi/datamodel-code-generator` | 99 MB | `7ae9017` | `README.md:327` `--url ... --output model.py` |
| N17 | **TS contract generation** (same OpenAPI → `types/robot.ts`) | `openapi-ts/openapi-typescript` | 90 MB | `d46a319` | generates `types`/`openapi-fetch` |
| N18 | unit-test framework (audit #18 "numbers must not repeat") | `pytest-dev/pytest` | 12 MB | `4fa645f` | official |
| N19 | frontend testing (E2E actually runs the wall) | `microsoft/playwright` | 108 MB | `d9f2fd3` | official |
| N20 | component testing + Mock (audit #23/#31) | `vitest-dev/vitest`, `testing-library/react-testing-library`, `mswjs/msw` | 70/1.4/6.9 MB | — | official |

### 6.2 Data lifecycle & disaster recovery (fill audits #7/#28)

| # | Requirement point | Repo | Size | Key evidence |
|---|---|---|---|---|
| N21 | retention/compression/continuous-aggregate (a ~130 GB time bomb per day) | `timescale/timescaledb` | 64 MB | `add_retention_policy` / `add_compression_policy` body |
| N22 | advanced time-series functions (gapfill etc., usable by dashboard) | `timescale/timescaledb-toolkit` | 3.9 MB | extended SQL functions |
| N23 | PG backup | `pgbackrest/pgbackrest` (**source repo, not image repo**) | 22 MB | official source. ⚠️ 2026-10-11 measured `pgbackrest/pgbackrest` **has no Docker image** (Docker Hub returns object not found); DR image must be self-built on the PGDG package, see `infra/ops/pgbackrest/Dockerfile` and `docs/DR-RUNBOOK.md §1` |
| N24 | WAL archive backup (cloud object storage) | `wal-g/wal-g` | 35 MB | official |

### 6.3 Replacement evaluation (**"even a full replacement must find something more perfect"**)

| Current choice | Candidate replacement | Conclusion | Basis |
|---|---|---|---|
| `emqtt-bench` (Erlang, only spits numbers, hard to write assertions) | **`k6` + `xk6-mqtt`** | ✅ **suggest replacing/using alongside** | k6 is Go with built-in JS assertions + official releases; `emqtt-bench` needs OTP 27.2+ to compile. R-07 wants "judgeable", k6 satisfies directly |
| `mock_fleet.py` purely in-house | `k6` can also generate load | ➖ **keep in-house** | only in-house can precisely produce the 24-joint real payload (I-4 judgment unchanged) |
| `grafana/oncall` (recommended in a prior round) | — | ❌ **withdrawn** | **proven: `README.md:3` original text "entered maintenance mode on 2025-03-11 and was archived on 2026-03-24"** |
| no on-call/escalation mechanism | `keephq/keep` | ✅ newly introduced | alive, multi-provider, docker-compose runnable |
| `zustand` (decided not to upgrade) | `tanstack/query` | ➖ **complement, not replace** | query manages server state/cache/retry, complements zustand's client-state management, not a replacement |
| no error boundary (audit #23) | `react-error-boundary` | ✅ small-cost direct use | 2.4 MB, official recommended posture |
| `alert_engine` in-house rule engine | `prometheus/prometheus` rules | ❌ **do not replace** | the rule engine judges "robots", Prometheus judges "the platform". **Same name, different layer** (concluded in a prior round) |
| in-house `deduplicator.py` | Alertmanager `inhibit_rules` | ➖ learn semantics, don't swap implementation | already copied semantics per I-5; pulling in an Alertmanager instance adds an ops layer |

### 6.4 Pitfalls **discovered this round** (empirical, not speculation)

1. **`victoriametrics/victoria-metrics` is a 404** (`curl -sI` → `HTTP/2 404`). A prior round's `INTEGRATION-PLAN.md` already noted "ls-remote failed, struck through", **this round re-confirmed it's a dead link**, not a transient network issue. The correct repo is `VictoriaMetrics/VictoriaMetrics` (1777 refs) — but we **don't introduce it** (judgment: overlaps with Prometheus, see §5.5).
2. **Grafana OnCall OSS has been archived** (see §5.3).
3. `git ls-remote` occasionally returns `refs=0` for large repos (e.g. first run on `grafana/loki`); **must re-check**, otherwise a live repo gets misjudged as dead — this is a pitfall of "reachability checking" itself.

### 6.5 Searched but **explicitly not downloaded** (new this round, with reasons)

| Repo | Size | Reason not to download |
|---|---|---|
| `thingsboard/thingsboard` | 404 MB | **whole platform (alternative)**, not a dependency; introducing it overturns the existing four-layer architecture |
| `openremote/openremote` | 106 MB | same as above; downloaded only for **architecture comparison** (multi-tenant/fleet model), not adopted |
| `apache/bifromq` | 17 MB | distributed MQTT broker, **replacement for EMQX**; a 100-robot scale doesn't need distributed |
| `nanomq/nanomq` | 33 MB | edge broker, **replacement for EMQX**; EMQX's ecosystem (Dashboard/Rule) is more complete |
| `illacloud/illa-builder` | 52 MB | low-code dashboard, **replacement for the in-house wall**; the deliverable is the in-house 2D dashboard, so not used |
| `grafana/grafana` | 2064 MB | judged not to download in a prior round; this round **maintains** that |
| `grafana/loki` + `vectordotdev/vector` + `fluent/fluent-bit` | 442+73+243 MB | log stack. **Real value** (audits #27/#19 want "logs to check"), but 700+ MB for a "how to configure" doc isn't worth it — **record recommended config instead**, spin up a service from the official image when needed |
| `openstatusHQ/openstatus` | 218 MB | status page is an **external-facing** product requirement; this project is an internal wall, so it's "value-add" not "essential" — downloaded for archive, listed as P3 |
| `chakra-ui/ark` | 134 MB | headless UI primitives, **replace hand-written components**; current Tailwind hand-writing already passes tsc, benefit < regression risk |
| `goauthentik/authentik` | 157 MB | SSO; Keycloak/Casdoor/this one, picking one is enough, choose the lighter |

> ⚠️ **Self-correction this round**: the first scan downloaded `ark-ui`/`fluent-bit`/`loki`/`authentik` (~1 GB total); after review we **deleted them per the repo's existing principle (don't introduce fatal-size / alternative solutions)**, keeping only the attribution conclusions. Downloaded ≠ should-have-been-downloaded; this also applies to NPCs.

---

## Candidate clone commands (if adopting, add to fetch-sources.sh first then run — currently not cloned to this repo)

```bash
cd external
# observability
git clone --depth 1 https://github.com/grafana/alloy.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-collector.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-python.git
git clone --depth 1 https://github.com/open-telemetry/opentelemetry-js.git
git clone --depth 1 https://github.com/prometheus-community/postgres_exporter.git
git clone --depth 1 https://github.com/oliver006/redis_exporter.git
git clone --depth 1 https://github.com/grafana/k6.git
git clone --depth 1 https://github.com/pmalhaire/xk6-mqtt.git
# auth / alert closed-loop
git clone --depth 1 https://github.com/fastapi-users/fastapi-users.git
git clone --depth 1 https://github.com/keycloak/keycloak.git
git clone --depth 1 https://github.com/casdoor/casdoor.git
git clone --depth 1 https://github.com/keephq/keep.git
git clone --depth 1 https://github.com/prometheus/alertmanager.git
git clone --depth 1 https://github.com/idealista/prom2teams.git
# contract-as-code
git clone --depth 1 https://github.com/koxudaxi/datamodel-code-generator.git
git clone --depth 1 https://github.com/openapi-ts/openapi-typescript.git
# testing and quality
git clone --depth 1 https://github.com/pytest-dev/pytest.git
git clone --depth 1 https://github.com/microsoft/playwright.git
git clone --depth 1 https://github.com/vitest-dev/vitest.git
git clone --depth 1 https://github.com/testing-library/react-testing-library.git
git clone --depth 1 https://github.com/mswjs/msw.git
# data lifecycle / disaster recovery
git clone --depth 1 https://github.com/timescale/timescaledb.git
git clone --depth 1 https://github.com/timescale/timescaledb-toolkit.git
git clone --depth 1 https://github.com/pgbackrest/pgbackrest.git
git clone --depth 1 https://github.com/wal-g/wal-g.git
```

### This round's verification boundaries (honestly labeled)

| Conclusion | Status |
|---|---|
| 20+ candidates **evaluated** (research only, **not cloned to this repo**; to adopt, add to `fetch-sources.sh` first) | ➖ evaluated |
| `DATA_SOURCE_NAME` / `REDIS_ADDR` / `/metrics` line numbers | ✅ measured (`grep -n`) |
| `fastapi-users 15.0.5` and `fastapi>=0.65.2` compatibility range | ✅ measured (source) |
| Grafana OnCall archived | ✅ measured (`README.md:3` original) |
| `victoriametrics/victoria-metrics` 404 | ✅ measured (`curl -sI`) |
| **No new-source service was run end-to-end** | ❌ no Docker environment; k6/keep/OTel not started |
| **`fastapi-users` not actually installed** | ❌ not runtime-integrated with `fastapi==0.110.0` |
| whether `xk6-mqtt` is compatible with the k6 v2 line | ❌ not compiled (needs Go) |

---

## 7. Reference sources actually cloned but not individually listed above (supplemental reconciliation)

> For full reconciliation with disk and `fetch-sources.sh`: §5's table lists 14, this repo's disk has 22 total (see the `SOURCES` array in `fetch-sources.sh` + `alertmanager`). The following **8** are declared and actually cloned in `fetch-sources.sh` but not expanded individually in §1–§5; recorded here:

| Repo | Belongs to layer | Purpose / Adoption conclusion |
|---|---|---|
| `locustio/locust` | stress | optional load tool for Order 06, complements `emqtt-bench`/`mock_fleet.py` (I-4) |
| `python-websockets/websockets` | backend runtime | `aiomqtt`'s lower-level WS contract check; this repo's WS push reference |
| `Kludex/uvicorn` | backend runtime | ASGI server contract check (upstream moved to `Kludex/uvicorn`) |
| `sqlalchemy/sqlalchemy` | backend runtime | ORM contract check (`alembic`'s foundation) |
| `pydantic/pydantic-settings` | backend runtime | config-layer contract check |
| `prometheus/node_exporter` | infrastructure | host-metrics exporter (already referenced by compose) |
| `timescale/timescaledb-docker` | infrastructure | TimescaleDB image/init contract (already referenced by compose) |
| `timescale/timescaledb-tune` | infrastructure | PG/TimescaleDB tuning-tool contract |

> **Reconciliation conclusion**: disk 22 = `fetch-sources.sh` 22 = §5(14) + §7(8). The ~30 listed in §6 are **evaluation candidates**, not on disk.
