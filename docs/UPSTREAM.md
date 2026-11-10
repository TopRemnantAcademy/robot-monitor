# Upstream Manifest

> Purpose: Every external capability in this repo can be traced to "which repo, which version,
> which file, which license". Companion docs: `external/SOURCES.md` (what was downloaded and why),
> `external/INTEGRATION-PLAN.md` (where it is applied line by line). The directory itself is
> gitignored (third-party source does not enter the main repo), but **these three attribution
> docs are committed**.

## 1. Runtime dependencies (introduced via package manager, source not cloned)

| Component | Version | License | Purpose |
|---|---|---|---|
| fastapi | 0.110.0 | MIT | Backend HTTP/WS |
| uvicorn | 0.28.0 | BSD-3 | ASGI server |
| pydantic / pydantic-settings | 2.6.4 / 2.2.1 | MIT | Data-contract validation |
| asyncpg | 0.29.0 → **0.32.0** (order 08) | Apache-2.0 | PG async driver |
| sqlalchemy | 2.0.28 | MIT | ORM |
| redis[hiredis] | 5.0.3 | MIT | State cache / Pub-Sub |
| aiomqtt | 2.5.1 | BSD-3 | MQTT client |
| websockets | 12.0 | BSD-3 | WS |
| react / react-dom | 18.3.1 | MIT | Frontend |
| echarts | 5.5.1 | Apache-2.0 | Line charts |
| echarts-for-react | 3.0.2 | MIT | React bridge |
| zustand | 4.5.5 | MIT | Frontend state |
| tailwindcss | 3.4.17 | MIT | Styling |

> The `aiomqtt` repo moved from `sbtinstruments/aiomqtt` to `empicano/aiomqtt` (PyPI package
> name unchanged).

## 2. Ops dependencies (container images, docker-compose `ops` profile)

| Image | Version | License | Reason for inclusion |
|---|---|---|---|
| `emqx/emqx` | 5.6 | Apache-2.0 (NOASSERTION, defer to repo LICENSE) | MQTT broker |
| `timescale/timescaledb` | 2.13.1-pg16 | Apache-2.0 + PostgreSQL | Time-series DB |
| `redis` | 7.2-alpine | RSALv2/SSPLv1 (**non-OSI**) | Cache; only called as a standalone container, no copyleft reach |
| `prom/prometheus` | v3.1.0 | Apache-2.0 | Metrics TSDB |
| `emqx/emqx-exporter` | 0.2.3 | Apache-2.0 | Supplementary EMQX metrics |
| `prom/blackbox-exporter` | v0.26.0 | Apache-2.0 | HTTP/TCP probes |
| `prometheuscommunity/postgres-exporter` | v0.17.0 | Apache-2.0 | PG/time-series write observability |
| `oliver006/redis_exporter` | v1.67.0 | MIT | Redis observability |
| `grafana/grafana` | 11.5.0 | **AGPL-3.0** ⚠️ | Ops dashboard |

### ⚠️ AGPL copyleft reminder

`grafana/grafana`, `grafana/loki`, and `minio/minio` are all **AGPL-3.0**. This repo is MIT.
"Calling an unmodified official image" does not trigger AGPL's source-disclosure obligation;
but **if you modify Grafana's source and offer it as a network service**, you must publish the
modified source. This is only a risk register entry, not a current blocker.

## 3. Source downloaded for reference only, not in runtime (`external/`, gitignored)

| Repo | HEAD | Size | Purpose |
|---|---|---|---|
| emqx/mqtt-to-timescaledb | `ee24974` | 1.2 MB | EMQX deployment contract (image name corrected) |
| timescale/tsbs | `8323e59` | 2.9 MB | Stress-test methodology |
| emqx/emqtt-bench | `cb86b18` | 388 KB | Broker-layer stress test |
| empicano/aiomqtt | `c8f8248` (v2.5.1) | 872 KB | Client contract |
| pmndrs/zustand | `d7a5583` | 3.6 MB | Version check |
| redis/redis-py | `22d47b5` | 18 MB | Version check |
| fastapi/full-stack-fastapi-template | `7257e60` | 3.6 MB | Project structure |
| prometheus/alertmanager | `e7a4afd` | 892 KB | Alert-inhibition design |
| prometheus/client_python | `9cd073c` | 14 MB | Metrics exposure (not wired in) |
| MagicStack/asyncpg | `13b0191` | 2.0 MB | Version check |
| hustcc/echarts-for-react | `2346edc` | 372 KB | Frontend bridge check |
| Kludex/starlette | `96d5023` | 7.1 MB | WS contract |
| sqlalchemy/alembic | `1bc21b8` | 4.1 MB | Migration approach |
| eclipse-paho/paho.mqtt.python | `b48baee` | 1.4 MB | Underlying client |
| **emqx/emqx** (sparse `apps/emqx_prometheus/`) | `7537a354` | 444 KB | **Prometheus endpoint evidence** |
| **emqx/emqx-exporter** | `dc299c94` | 2.3 MB | **Ops-layer job/dashboard templates** |
| **prometheus/prometheus** | `2213c3c1` | 33 MB | **scrape config and promtool contract** |
| **prometheus/blackbox_exporter** | `ea6d6cd6` | 736 KB | **Probe modules** |
| **prometheus-community/postgres_exporter** | `fb6be9d8` | 1.1 MB | **PG observability scope** |
| **oliver006/redis_exporter** | `6de1e8b0` | 916 KB | **Redis observability** |
| **benfred/py-spy** (sparse README) | `b870defa` | 36 KB | **Flame graphs during stress test (needs SYS_PTRACE)** |
| **pgbackrest/pgbackrest** | `ad72e581` | 19 MB | **Backup config contract (not wired)** |
| **minio/minio** | `7a2a2a2c` | 40 MB | Backup target object store (fallback) |
| **OpenBMB/AgentVerse** | `f90c4bd9` | 31 MB | Multi-robot task-solving framework (LLM; **G-7 evaluated, not adopted**) |

> ⚠️ **AgentVerse verdict (Issue #114 / G-7)**: empirically an LLM text-dialogue framework;
> `BaseAgent` forces `llm: BaseLLM` (`agentverse/agents/base.py:21`), and `step()` returns a
> natural-language `Message`, producing **no** telemetry values for `mock_fleet`.
> Without `OPENAI_API_KEY`, even `import agentverse` crashes because `DEFAULT_CLIENT` is
> undefined. **Not adopted**: it pins `pydantic==1.10.7` / `openai==1.1.0` / `fastai==0.95.1`,
> conflicting with this repo's pydantic v2 stack; and the 10Hz × 100-machine LLM round-trip
> latency is orders of magnitude off. Task-level simulation is handled by `simulator/task_sim.py`.
> Full evidence (original errors + JSON report) is in `docs/agentverse-verification/README.md`. |

## 4. Explicitly **not** included

| Repo | Size | Reason |
|---|---|---|
| grafana/grafana (full) | 2011 MB | Only the provisioning directory is used as reference, covered by `grafana-provisioning-ref` |
| apache/echarts | 264 MB | Already pulled via npm |
| pydantic/pydantic | 458 MB | Already pulled via pip |
| timescale/timescaledb | 65 MB | Mostly C extension; SQL-layer contract already covered by official examples |
| fastapi/fastapi | 53 MB | Already pulled via pip |
| puppeteer | 1533 MB | Already pulled via npm |
| redis (C source) | 212 MB | Already covered by the image |
| Tencent/TAD_Sim | 643 MB / 354 MB clone | **Wanted to use TAD Sim for real-robot simulation, but size + GPU + ROS dependencies are not worth it; measured 354 MB after clone** |
| OpenBMB/ChatDev | 301 MB | Overlaps AgentVerse's capability; took the lighter one |
| debauchee/barrier | 27 MB | Soft KVM, only valuable for "real-robot operation"; not useful for telemetry monitoring |
| tsl0922/ttyd | 2.7 MB | Terminal in browser; this repo already uses Grafana as the ops window, redundant |
