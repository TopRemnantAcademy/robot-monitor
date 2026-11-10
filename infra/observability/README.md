# Observability wiring guide

> Answers one question: **once these open-source components are downloaded, where exactly are they wired in, and how do they run.**
> Provenance and trade-off rationale: `external/SOURCES.md` §5; adoption points with file:line: `external/INTEGRATION-PLAN.md` §6.

## 1. Bring it up with one command

```bash
docker compose \
  -f infra/docker-compose.yml \
  -f infra/docker-compose.app.yml \
  -f infra/observability/docker-compose.observability.yml \
  up -d
```

What each of the three layers manages:

| File | Contents | Why a separate layer |
|---|---|---|
| `infra/docker-compose.yml` | EMQX + TimescaleDB + Redis | the minimal set to "make the dashboard work", must stay lightweight |
| `infra/docker-compose.app.yml` | backend + alert-engine | the apps must be on the same network, otherwise Prometheus cannot resolve the target names |
| `infra/observability/…` | prometheus + alertmanager + 2 exporters | ops enhancement layer, stacked on demand |

## 1.1 Why exporters must share a network with the base components (Order 22 / I-12)

The target addresses of the four exporters/prometheus are all **Docker service DNS** (`timescaledb:5432`, `redis:6379`, `backend:8000` …). These names only resolve on **the same network**.

This repo's approach is to **declare `networks:` in none of the three layers**, letting Compose drop all services from the three files into the default network `{project}_default`. `verify_wiring.sh` step 5 guards this: the moment someone adds `networks:` in the observability file, the check immediately turns red.

The pit that was hit (exactly why I-12 keeps emphasizing this): the exporter container **comes up normally** and the port listens normally, but `/metrics` has no business metrics at all — because `timescaledb` in `DATA_SOURCE_NAME` fails to resolve. Looking only at `docker ps` completely misses it.

When assembling the command the project name must be consistent: use **one** `docker compose -f a -f b up`, or add the same `-p <project>` to every command. Otherwise two networks are created and DNS cannot see across them.

## 2. Ports and entry points

| Service | Address | Purpose |
|---|---|---|
| backend | `:8000` | `/healthz`, `/robots`, `/ws/robots`, **`/metrics`** (all unauthenticated; business REST like `/robots` needs Bearer) |
| alert-engine | `:8001` | **`/metrics`** (standalone HTTP service, not FastAPI) |
| Prometheus | `:9090` | UI / PromQL / `/-/rules` |
| Alertmanager | `:9093` | UI / silence / inhibition |
| postgres-exporter | `:9187` | TimescaleDB metrics |
| redis-exporter | `:9121` | Redis metrics |

## 3. Scrape topology

```
prometheus.yml
├── prometheus                 localhost:9090      自身健康
├── robot_backend              backend:8000/m      backend_ingestion/app/metrics.py
├── robot_alert_engine         alert-engine:8001/m alert_engine/app/metrics.py
├── emqx                       emqx:18083/api/v5/prometheus/stats
├── postgres                   postgres-exporter:9187/m
└── redis                      redis-exporter:9121/m
```

## 4. Alert rules are in two layers, don't confuse them

| Layer | Whose | Judges | Falls where |
|---|---|---|---|
| App layer | `alert_engine` | **robots**: overheating / low battery / offline / joint stuck | `robot_alert` table + Redis `robot:alerts` |
| Platform layer | Prometheus + Alertmanager | **the monitoring system itself**: broker down / store backlog / scrape failure | `rules/robot-alerts.yml` |

`robot_alert` answers "which robot broke"; Prometheus answers "does the monitoring system still deserve to answer the previous question". When the latter fails, all conclusions of the former are untrustworthy.

### 4.0 Single source of naming (ISSUE #72)

The backend metric prefix is **only `robot_backend_*`**. Historically the Order 16 implementation branch set up a separate `robot_monitor_*` set (6 of them), running in parallel with the Order 19-landed `robot_backend_*` — of which 3 were duplicate metrics from the same counting source (same `queue.qsize()` / `writer.written` / `mqtt.received`). Now unified:

| Old name (retired) | Status |
|---|---|
| `robot_monitor_batch_queue_size` | removed (same source as `robot_backend_queue_depth`) |
| `robot_monitor_batch_rows_total` | removed (same source as `robot_backend_telemetry_written_total`) |
| `robot_monitor_mqtt_messages_total` | removed (same source as `robot_backend_mqtt_received_total`) |
| `robot_monitor_batch_flush_total{reason}` | merged into `robot_backend_batch_flushes_total{reason}` |
| `robot_monitor_ws_connections` | renamed to `robot_backend_ws_connections` |
| `robot_monitor_history_latency_seconds` | renamed to `robot_backend_history_latency_seconds` |

**If any panel/alert referenced `robot_monitor_*`, it must be renamed in sync** (this repo's rules and verify_wiring are already synced; external Grafana panels are outside the repo and are an ops-side action). The two renamed metrics keep the same PromQL syntax, only the name changed.

#### 4.0.1 Order 32 closure: from 'docs say it changed' to 'a machine can prove it changed'

The table above is a **record**, not a **gate**. Order 32 (ISSUE #100) adds the gate itself:

| Layer | Gate | Location | Can it go red |
|---|---|---|---|
| Unit test | `FORBIDDEN_MONITOR_PREFIX` prefix assertion | `backend_ingestion/tests/test_observability.py` | Yes (in-process registry) |
| Static | whole-repo scan + name-by-name check (old name gone / new name present) | `infra/observability/verify_wiring.sh` [3.6] | Yes (measured: injecting the old name FAILs) |
| **Runtime** | real `/metrics` byte assertion | `tests/e2e/test_e2e_30k.py --metric-naming-check` (CI's e2e stage) | Yes (exit code 1 → stage red) |

Why the runtime layer is needed: unit and static gates both look at the problem **in source / in-process**, but `robot_monitor_*` is the kind of thing that "revives if you register one more Collector" — only by scraping the bytes emitted by **the actually-running process** do you know "the panel really can't see the old name".

One rename synced on the alert-engine side (outside #72's convergence scope): `robot_alert_robots_online` → `robot_alert_robots_online_gauge`. The backend-side `robot_backend_robots_online` is **left alone** — it is referenced by production PromQL, and renaming it would be cross-unit rework.

### 4.1 The two 'WS metrics' are not the same thing (easy to confuse)

| Metric | Perspective | Who can answer | Who writes |
|---|---|---|---|
| `robot_backend_ws_connections` | **Server side** | how many clients are connected | incremented/decremented on connect/disconnect in `main.py`'s `ws_robots` |
| `robot_backend_ws_probe_alive` | **Client side** | can this connection still actually receive the first snapshot frame | the synthetic probe loop in `app/ws_probe.py` |
| `robot_backend_ws_probe_failures_total` | **Client side** | how many times it dropped (`increase()` shows jitter) | same as above |

Criterion: **connection count ≥ 1 and probe is 0** ⇒ the dashboard is "fake green" — the gateway stalled an upgrade / the snapshot threw / Redis Pub/Sub dropped, but the connection object remains. This is exactly the incident gap G-3 targets. The probe is unauthenticated (self-signed token hitting loopback), runs ≥30s once, and is degradable (`WS_PROBE_ENABLED=false` = no-op).

## 5. Quick self-check

```bash
# 1) the three exporters' /metrics all have content
curl -sf localhost:8000/metrics | grep robot_backend_queue_depth
curl -sf localhost:8001/metrics | grep robot_alert_robots_online_gauge
curl -sf localhost:9187/metrics | head

# 2) all Prometheus targets UP (don't just check containers are running)
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"' | sort | uniq -c

# 3) rules loaded
curl -s localhost:9090/api/v1/rules | grep -o '"name":"[A-Za-z]*"' | head
```

Criterion: all `health` are `up`. When a `down` appears, troubleshoot per below.

## 6. Common failures

| Symptom | Root cause | Action |
|---|---|---|
| `robot_alert_engine` always down | the alert-engine process didn't start the metrics thread | confirm `alert_engine/app/main.py`'s `main()` calls `metrics.start()` |
| `robot_backend` always down | the app runs on the host, `backend` domain can't resolve | use `docker-compose.app.yml` to put the app in the network |
| `emqx` down | `/api/v5/prometheus/stats` needs an API key | that endpoint is unverified; don't fake-report up=1, use emqtt-bench `--prometheus` instead |
| `queue_depth` unchanged after scrape | the Gauge is only set on write | already fixed: the `/metrics` handler refreshes before scrape (`main.py:86`) |
| postgres/redis-exporter up but no business metrics | the exporter and timescaledb/redis are **on different networks**, `DATA_SOURCE_NAME`'s host fails to resolve | confirm none of the three compose files declare `networks:`, and that they are brought up with one command (or the same `-p`) |
| error at startup `POSTGRES_PASSWORD not set` | `${POSTGRES_PASSWORD:?…}` by design blocks credential-less startup | on the host `cp .env.example .env` and fill it in (see the TimescaleDB credentials section in `.env.example`) |
| `robot_backend_ws_probe_alive` always 0 | probe target wrong / port wrong / service behind a gateway | the probe defaults to `ws://127.0.0.1:${WS_PORT}${WS_PROBE_PATH}`; inside a container it must match `--port`; to disable the probe use `WS_PROBE_ENABLED=false` (then the metric does not appear) |
