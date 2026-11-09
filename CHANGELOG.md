# Changelog

All notable changes to RobotMonitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Contributor-facing docs: `CONTRIBUTING` in **English / Korean / Chinese** (`CONTRIBUTING.md`, `CONTRIBUTING.ko.md`, `CONTRIBUTING.zh.md`).
- `ROADMAP.md` with a phased plan and an explicit **dependency policy** (which pins are deliberate holds and why).
- `CODEOWNERS`, `.github/dependabot.yml` (with coordinated-release groups for OpenTelemetry & pydantic), and issue/PR templates.
- GitHub Discussions seeded: a **Welcome** thread and an **Ideas** category for proposals.
- 10 starter issues labelled `good first issue` / `help wanted` covering i18n, a11y, dark mode, CSV export, Slack notifier, Grafana example, one-command compose, and component tests.

### Fixed
- **i18n infinite re-render** — stabilized the fallback `useI18n().t` so the dashboard no longer enters an async render loop that hung CI (`e0a0a7c`).
- **"1 models" pluralization** — the supported-models header now shows `1 model` / `N models` correctly across en/ko/zh (`5626176`).
- **Responsive supported-models strip** — cards grow to fill width; the demo catalog shows two models so the row reads naturally.

### Changed
- **Dependencies (safe minor refresh)** — `redis` 8.1.0, `websockets` 17.4, `prometheus-client` 0.26.0, `aiomqtt` 2.5.1, `rich` 15.0.0, `@tanstack/react-query` 5.106.1, `echarts-for-react` 3.0.6, `@testing-library/jest-dom` 7.0.1, `happy-dom` 21.0.4. All individually green on CI.
- **CI hardening** — `backend` and `contract-gate` jobs install `backend_ingestion` + `alert_engine` requirements into one venv; both files now pin `pydantic==2.14.3` and `pydantic-settings==2.16.2` (lockstep), and a new guard fails fast if the pins ever diverge (`6455339`).
- **Dependabot hygiene** — explicit ignores for `bcrypt`, `setuptools`, `fastapi`, `fastapi-users`, `uvicorn` (each is a deliberate hold: bumping breaks `passlib → fastapi-users` at runtime, or is a breaking 0.x major).

### Held on purpose
`bcrypt 4.0.1`, `setuptools<81`, `fastapi-users 12.1.3`, `uvicorn 0.28.0`, `opentelemetry 1.22.0 / 0.43b0`.
Rationale is in `ROADMAP.md → Dependency policy`.

---

## [0.1.0] — Public launch

### Added
- Open-source operations dashboard for a 100-robot humanoid fleet (10 Hz telemetry).
- **Ingestion backend** (`backend_ingestion`): MQTT → TimescaleDB + Redis, batch writer, WebSocket/REST gateway.
- **Alert engine** (`alert_engine`): 4 rules (motor temp / battery low / offline / joint stuck) with Redis-backed deduplication.
- **Simulator** (`simulator`): 100-robot mock fleet producing realistic telemetry.
- **Dashboard** (`frontend_dashboard`): React 18 + Vite + ECharts fleet wall, robot detail drawer, joint heatmap, live alert stream, **i18n (en / ko / zh)**.
- **Infra**: docker-compose stack (EMQX + TimescaleDB + Redis + services), TimescaleDB schema/init.
- **CI**: GitHub Actions mirroring the CNB test gates (frontend / backend / e2e-30k / contract-gate).
- MIT license.

[Unreleased]: https://github.com/TopRemnantAcademy/robot-monitor/compare/v0.1.0...main
[0.1.0]: https://github.com/TopRemnantAcademy/robot-monitor/releases/tag/v0.1.0
