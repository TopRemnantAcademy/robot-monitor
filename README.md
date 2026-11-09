# RobotMonitor — Live monitoring for a 100-robot humanoid factory

RobotMonitor is an open-source operations dashboard that watches **100 humanoid robots** on a factory floor in real time: online/offline status, battery, motor & joint temperatures, torque, fault codes, and live alerts.

High-frequency telemetry (100 robots × 10 Hz = 1000 msg/s) flows over MQTT → a FastAPI ingestion service that batch-writes to **TimescaleDB**, caches the latest state in **Redis** (Hash + TTL + Pub/Sub), and streams it to the React dashboard over **WebSocket**. An alert engine subscribes to the stream, evaluates rules, and pushes alerts to the UI.

> 🌍 **This project is international.** Documentation is available in:
> - 🇺🇸 **English** (this file)
> - 🇰🇷 [한국어](./README.ko.md)
> - 🇨🇳 [中文](./README.zh.md)
>
> The dashboard UI itself supports **English / 한국어 / 中文 / 日本語** with a live language switcher (default: English). See [docs/README.md](./docs/README.md).

<p align="center">
  <a href="https://github.com/TopRemnantAcademy/robot-monitor/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TopRemnantAcademy/robot-monitor/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="i18n EN|KO|ZH|JA" src="https://img.shields.io/badge/i18n-EN%20%7C%20KO%20%7C%20ZH%20%7C%20JA-blue">
  <img alt="Stack FastAPI|TimescaleDB|React" src="https://img.shields.io/badge/stack-FastAPI%20%7C%20TimescaleDB%20%7C%20React-9cf">
  <img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen">
</p>

---

## Preview

<p align="center">
  <img src="docs/assets/dashboard-preview.png" alt="RobotMonitor dashboard — 100-robot fleet card wall with live alerts" width="820"/>
  <br/>
  <em>Live fleet view — 100 humanoid robots with status / battery / temperature and a real-time alert stream.</em>
</p>

<p align="center">
  <img src="docs/assets/robot-detail-drawer.png" alt="Robot detail drawer with joint-temperature chart and alert history" width="820"/>
  <br/>
  <em>Per-robot detail drawer — joint-temperature chart, recent history, and alert timeline.</em>
</p>

> Want to see it live without Docker? Run the **mock backend** — see [Quick start (frontend only)](#quick-start-frontend-only--no-docker-needed).

---

## Architecture

```
Robot fleet (Edge) --MQTT--> [backend_ingestion] --batch--> TimescaleDB
                            |-- Redis (Hash + TTL) + Pub/Sub
[alert_engine]   --subscribe stream--> rule evaluation --> robot_alert + UI alert stream
[frontend_dashboard] <--WebSocket /ws/robots-- [backend_ingestion]
[simulator]      --MQTT--> emulates 100 robots @10Hz + fault injection (load testing)
```

| Path | What it is |
| :--- | :--- |
| `docs/` | Architecture write-up, developer guide, API contracts (`openapi`, `ws-envelope`), and audit notes |
| `infra/` | `docker-compose.yml` (EMQX + TimescaleDB + Redis), `init.sql` (hypertables + continuous aggregates) |
| `backend_ingestion/` | FastAPI ingestion + storage service (serves `/ws/robots` and REST) |
| `alert_engine/` | Alert / health rule engine |
| `frontend_dashboard/` | React dashboard (Vite + ECharts + TanStack Query + zustand) |
| `simulator/` | 100-robot telemetry simulator with fault injection |
| `test_integration.sh` | End-to-end smoke script |

---

## Quick start (full stack, Docker)

```bash
docker compose -f infra/docker-compose.yml up -d   # start middleware (EMQX, TimescaleDB, Redis)
bash test_integration.sh                            # end-to-end smoke test (build modules first)
```

Then open the dashboard dev server:

```bash
cd frontend_dashboard
npm install
npm run dev        # Vite dev server on http://localhost:5173
```

---

## Quick start (frontend only — no Docker needed)

If you cannot run Docker in your environment, the dashboard can run against a **mock backend** that emulates the WebSocket + REST contract without any infrastructure:

```bash
cd frontend_dashboard
npm install
node mock_backend.mjs            # mock WS + REST server on :8787
VITE_API_BASE=http://localhost:8787 VITE_WS_HOST=localhost VITE_WS_PORT=8787 npm run dev   # Vite on :5173, talking to the mock
```

The mock backend is the recommended path for **frontend contributors** and CI-local testing on machines without Docker. See [docs/developer-guide.md](./docs/developer-guide.md) for details.

---

## Internationalization (i18n)

The dashboard ships with English (default), Korean, Chinese, and Japanese. UI chrome strings live in [`frontend_dashboard/src/i18n/locales.ts`](./frontend_dashboard/src/i18n/locales.ts) and are resolved through the `useI18n()` hook; a language switcher lives in the top header. **Server-supplied data** (alert messages, robot names) is intentionally *not* translated.

To add a language: add a `Locale` entry and its string dictionary in `locales.ts`; the switcher in the top header picks it up automatically from the `LOCALES` array.

---

## Contributing

We welcome contributors of every level — from first-time PRs to robotics-domain experts. Please read:

- 📖 [CONTRIBUTING.md](./CONTRIBUTING.md) — how to set up, branch, and open a PR
- 📐 [docs/developer-guide.md](./docs/developer-guide.md) — architecture, dev workflow, and the mock-backend path
- 🤝 [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — community standards
- 🔒 [SECURITY.md](./SECURITY.md) — how to report vulnerabilities
- 🗺️ [ROADMAP.md](./ROADMAP.md) — where we are and where to plug in
- 💡 Propose a feature via a **Feature request** issue or **Discussions · Ideas**

Good first issues are tagged [`good first issue`](https://github.com/TopRemnantAcademy/robot-monitor/labels/good%20first%20issue); bigger tasks are tagged [`help wanted`](https://github.com/TopRemnantAcademy/robot-monitor/labels/help%20wanted).

---

## License

[MIT](./LICENSE) — free for commercial and personal use.
