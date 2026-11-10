# Competitive analysis — robot-fleet monitoring / observability

> Benchmark of open-source / open-core projects in the same direction as
> **RobotMonitor** (real-time monitoring for a 100-humanoid-robot factory floor).
> Sources were gathered on 2026-11-10 from public project sites and repositories.
> This document is the foundation for the roadmap items A/B/C (real-robot bridge,
> statistical anomaly detection, offline store-and-forward buffer).

## 1. Same-direction projects

| Project | Core | Stack | License | Maturity / scale |
| :--- | :--- | :--- | :--- | :--- |
| **OpenRobOps (ORO)** · InOrbit | Fleet mgmt + operations, **ISO 21423 reference implementation** | Meteor 3 + React 18 / MongoDB / Mosquitto MQTT / ROS 2 · Open-RMF | Apache-2.0 | Thousands of robots in production; ROS co-founder (S. Cousins), Open Robotics CTO backing |
| **BotBrain (BBOSS)** | Open hardware + dashboard + ROS 2, **humanoid** (Unitree G1/Go2, Tita) | Next 15 + React 19 / ROS 2 Humble / Jetson | OSS | Community |
| **Netdata** | Edge-native observability, **edge ML anomaly detection** | Per-robot agent / own TSDB | Open agent (commercial cloud) | Commercial + open, many robots |
| **Flotilla** · Equinor | Industrial inspection fleet ops, **real CCS-site deployment (ANYmal)** | React + Vite / ASP.NET / Mosquitto / Postgres / OTel | EPL-2.0 | 3 environments (Aurora), live field |
| **jragni/robot-telemetry-dashboard** | ROS 2 + WebRTC telemetry, **extreme engineering rigor** | React 19 + Vite 7 + Canvas + Zustand / rosbridge + CBOR | MIT | Personal, 476 unit + Playwright tests |
| **absolutely-sharad/Robot_Telemetry_Dashboard** | **Near-identical stack to RobotMonitor** | FastAPI + SQLite + Alembic + WS / React | MIT | Personal, mock data |
| youngroma / Protocup | Simple Flask+React dashboard / RoboCup humanoid (C++/UDP) | Flask+React / C++·Python·UDP | MIT | Personal / education |

Sources: `openrobops.org`, `github.com/OpenRobOps`, `github.com/equinor/flotilla`,
`netdata.cloud/solutions/industries/robotics`, `github.com/jragni/robot-telemetry-dashboard`,
`github.com/botbotrobotics/BotBrain`.

## 2. What they do better (RobotMonitor gaps)

| Dimension | RobotMonitor today | Where competitors lead |
| :--- | :--- | :--- |
| **Real-robot integration** | Simulator / mock only (`simulator/mock_fleet.py` emits MQTT mocks) | ORO (ROS 2 / ISO 21423), BotBrain (Unitree humanoids), Flotilla (ANYmal live) connect to real robots — **biggest gap** |
| **Standardization / interoperability** | None | ORO's ISO 21423 reference implementation = vendor neutrality |
| **Anomaly detection / auto-remediation** | Static-threshold alert engine (`alert_engine/`) | Netdata (edge ML, 18 models/metric), ORO (incident remediation) detect → recover |
| **Maps / camera / teleop** | Number cards (ECharts) | ORO, BotBrain, jragni show maps, cameras, WebRTC — "see the robot" |
| **Offline resilience** | Centralized; no buffer documented | Netdata (store-and-forward), ORO (adaptive sampling) — no blind spots on network drop |
| **Engineering rigor** | contract-gate + e2e 30k | jragni: 476 unit + Playwright + 5-viewport smoke — thicker visual / regression coverage |
| **Community / industry backing** | Solo open source | ORO: OSRA, RobOpsCon, industry heavyweights |

## 3. What RobotMonitor defends (honest strengths)

- **Time-series optimization**: TimescaleDB + continuous aggregates + batch write handle
  `100 robots × 10 Hz = 1000 msg/s` honestly. ORO uses Mongo; sharad/jragni use SQLite (won't scale).
- **Low onboarding friction**: mock backend + frontend-only quick start — lighter than
  ORO/Flotilla (Azure / Entra / Key Vault setup).
- **i18n 4 languages (EN/KO/ZH/JA)** + live switcher — most competitors are English-only;
  strong positioning for a global open-source project.
- **Already present**: JWT/RBAC auth (`fastapi-users`), OpenTelemetry instrumentation, contract-gate, MIT
  (more permissive than ORO's Apache-2.0 or Flotilla's EPL-2.0).

## 4. Adoption priorities (what to borrow)

- **A. Real-robot bridge (highest priority)**: extend `simulator/` with a ROS 2 bridge and an
  ISO 21423-compatible envelope so the system is "real-robot-ready" → sheds the "simulator-only" label.
- **B. Anomaly-detection layer**: add per-robot baseline (z-score / rolling mean) auto-thresholding to
  `alert_engine/` (Netdata-style edge ML is overkill for our resource budget).
- **C. Offline buffer**: add store-and-forward (local queue + replay) to `backend_ingestion/` so a
  DB outage does not drop telemetry.
- **D. Spatial / trajectory view**: add a map panel (ECharts) to `frontend_dashboard/` beyond number cards.
- **E. Test thickness**: add jragni-style Playwright 5-viewport smoke + component tests to CI.
- **F. Config-as-Code**: externalize alert rules / telemetry sources to YAML + CLI apply (ORO-style).

## 5. Status

- **Implemented (this milestone)**: A (ROS 2 + ISO 21423 bridge + simulator emitter), B (anomaly rule),
  C (offline store-and-forward buffer). See `docs/robot-integration.md`, `bridge/`, and the
  `anomaly` / `offline_buffer` modules.
- **Planned (not yet)**: D (map panel), E (Playwright viewport smoke), F (config-as-code).
