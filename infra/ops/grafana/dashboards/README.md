# RobotMonitor Grafana Dashboards

This folder is auto-provisioned by Grafana via the `RobotMonitor` provider
(see `../provisioning/dashboards/dashboards.yaml`). The provider mounts this
directory (`/var/lib/grafana/dashboards`) into the `robot-monitor` folder
(`folderUid: robot-monitor`) in Grafana, so any `*.json` file dropped here is
imported automatically on startup (and re-imported every 30s if changed).

## Files

| File | Purpose |
| :--- | :--- |
| `robotmonitor-example.json` | **New, safe reference dashboard** — wiring the exporters (TimescaleDB/Postgres, Redis, EMQX) plus a direct TimescaleDB connection check. Uses the correct provisioned datasource uids. |
| `robot-alerts.json` | Alert-focused panels (backend health, alert rates). |
| `robot-backend.json` | Backend ingestion / telemetry panels. |
| `authentication.json`, `authorization.json`, `client-events.json`, `emqx-self-details.json`, `emqx-self-overview.json`, `messages.json`, `overview.json`, `rule-engine-count.json`, `rule-engine-rate.json` | EMQX exporter dashboards. |

## The example dashboard (`robotmonitor-example.json`)

It is a minimal, import-clean reference showing how to wire panels to the
**provisioned** datasources. It contains four panels:

1. **TimescaleDB (Postgres) up** — `up{job="timescaledb"}` (Prometheus, `rm-prometheus`).
2. **Redis up** — `up{job="redis"}` (Prometheus, `rm-prometheus`).
3. **EMQX up** — `up{job="emqx"}` (Prometheus, `rm-prometheus`).
4. **TimescaleDB connection check** — `SELECT 1 AS ok` via the Postgres/TimescaleDB
   datasource (`rm-timescaledb`, `type: postgres`).

All panels use `up{job="..."}` selectors, which always exist for any Prometheus
scrape target, so the dashboard imports cleanly and lights up green as soon as the
exporters are present — even before you know the exact EMQX/Redis metric names.

### Exporter dependencies

These panels assume the observability stack (`profiles: ["ops"]`) is running, which
provides:

- **postgres-exporter** — scraped with `job="timescaledb"`.
- **redis-exporter** — scraped with `job="redis"`.
- **EMQX exporter** — scraped with `job="emqx"`.
- **Prometheus** (`http://prometheus:9090`) — datasource uid `rm-prometheus`.
- **TimescaleDB (Postgres)** — datasource uid `rm-timescaledb`.

Start them with:

```bash
docker compose -f infra/docker-compose.yml --profile ops up -d
```

### Import steps

**Option A — auto-provisioning (recommended here):** just drop the JSON in this folder
and Grafana imports it into the `robot-monitor` folder automatically (no clicks needed).
Restart Grafana or wait up to 30s if it was already running.

**Option B — manual import:** in Grafana, go to **Dashboards → New → Import**, paste the
file contents or upload `robotmonitor-example.json`, and confirm the two datasources
(`RobotMonitor-Prometheus`, `RobotMonitor-TimescaleDB`) when prompted.

## ⚠️ Known datasource-uid mismatch in the existing `robot-*.json` dashboards

`robot-alerts.json` and `robot-backend.json` reference the Prometheus datasource as:

```json
{ "type": "prometheus", "uid": "prometheus" }
```

That is a **literal uid `"prometheus"`** which does **not** match the provisioned uid
`rm-prometheus` (defined in `../provisioning/datasources/datasources.yaml`). On a clean
provisioning, those panels therefore show **"Datasource not found"**.

This is a pre-existing bug and is **out of scope for this example dashboard** — it should be
fixed in a separate change (replace `"uid": "prometheus"` with `"uid": "rm-prometheus"` in
both files, and add the matching `"type": "prometheus"` where the object only has a uid).
The new `robotmonitor-example.json` deliberately uses the correct provisioned uids so it
works on a fresh install.
