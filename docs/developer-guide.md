# RobotMonitor Developer Guide

This guide explains the architecture, the local development workflow, and — importantly for
contributors without Docker — how to run the **frontend against a mock backend** with zero
infrastructure.

## System overview

```
Robot fleet (Edge) --MQTT--> [backend_ingestion] --batch--> TimescaleDB
                            |-- Redis (Hash + TTL) + Pub/Sub
[alert_engine]   --subscribe stream--> rule evaluation --> robot_alert + UI alert stream
[frontend_dashboard] <--WebSocket /ws/robots-- [backend_ingestion]
[simulator]      --MQTT--> emulates 100 robots @10Hz + fault injection
```

- **backend_ingestion** (FastAPI, `:8000`): ingests telemetry, serves `/ws/robots` (WebSocket) and
  REST endpoints (fleet state, alerts, supported-robots, history).
- **alert_engine** (`:8001`): subscribes to the telemetry/alert stream, evaluates rules, writes
  `robot_alert` rows, and feeds the UI alert stream.
- **frontend_dashboard** (React 18 + Vite 5 + ECharts + TanStack Query + zustand): the live board.
- **simulator**: generates 100-robot telemetry over MQTT for load testing and demos.
- **infra**: `docker-compose.yml` (EMQX + TimescaleDB + Redis) and `init.sql`
  (hypertables + continuous aggregates).

## Full-stack local run (Docker)

```bash
docker compose -f infra/docker-compose.yml up -d   # EMQX, TimescaleDB, Redis
bash test_integration.sh                            # end-to-end smoke (build modules first)
cd frontend_dashboard && npm install && npm run dev # http://localhost:5173
```

## Frontend-only local run (no Docker — recommended for UI contributors)

The dashboard can run without any middleware by pointing it at a **mock backend** that emulates the
live WebSocket + REST contract. This is the intended path on machines without Docker (e.g. CI
runners or sandboxes where the Docker daemon is unavailable).

```bash
cd frontend_dashboard
npm install

# Terminal A — mock backend (WS + REST) on :8787
node mock_backend.mjs

# Terminal B — Vite dev server on :5173 (proxies /ws and /api to :8787)
npm run dev
```

Open http://localhost:5173. The mock backend streams synthetic 100-robot telemetry and serves the
same endpoints the real backend does (`/ws/robots`, `/alerts`, `/supported-robots`, `/robots/:id/history`).
It is the contract-equivalent stand-in for EMQX + TimescaleDB + Redis, so all UI behavior —
KPIs, joint heatmap, torque bars, history charts, alert stream, acknowledge — is exercisable.

### Vite proxy config

The dev server proxies API traffic so the frontend code does not need to know whether it is talking
to the real backend or the mock. Check `vite.config.ts` (or `frontend_dashboard/vite.config.*`) for
the `server.proxy` block that maps `/api` and `/ws` to the target port.

### Switching the language

The language switcher in the top header persists the choice to `localStorage` (`rm-locale`). Default
is English. To verify Korean/Chinese rendering, just use the switcher — no rebuild required.

## Testing

From `frontend_dashboard`:

```bash
npm test            # vitest run (unit + component tests)
npm run typecheck   # tsc --noEmit
npx vite build      # production build + bundle-size guard (scripts/report-bundle-size.mjs)
```

From the repo root:

```bash
pytest -q --ignore=tests/e2e                 # backend unit tests
python -m pytest tests/e2e/test_metric_naming_probe.py -v
bash ci/contract-gate.sh                      # OpenAPI/types/WS envelope drift
bash ci/run-e2e.sh                            # real TimescaleDB 30k-landing e2e
```

The full gate is mirrored in [`.github/workflows/ci.yml`](./.github/workflows/ci.yml).

## Internationalization (for contributors)

- Never hard-code UI text. Add keys to `frontend_dashboard/src/i18n/locales.ts` with `en`/`ko`/`zh`
  values, and resolve via `useI18n()` (the `t(key, vars)` function, with English fallback).
- `frontend_dashboard/src/i18n/I18nProvider.tsx` — context + persistence + safe fallback (so isolated
  component tests render without a provider).
- `frontend_dashboard/src/i18n/LanguageSwitcher.tsx` — the header dropdown.
- `frontend_dashboard/src/i18n/format.ts` — locale-aware clock formatting.
- Server-supplied data (alert `message`, robot `display_name`) is **not** localized.

## Project conventions

- Code and comments in **English**.
- No secrets in commits; `.env` / `.env.local` / token files are git-ignored.
- Keep PRs scoped; document evidence in the PR body.
- CI must stay green (backend pytest, frontend `npm test` + typecheck + build + bundle guard,
  contract gate, real-DB e2e).
