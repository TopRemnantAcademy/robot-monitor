# Roadmap

RobotMonitor is an open, community-driven operations dashboard for humanoid robot fleets. This roadmap shows where the project is today and where contributors can plug in. It is a living document — propose changes via a **Feature request** issue or a **Discussions · Ideas** thread.

## Maturity snapshot (2026-11)

| Area | State | Evidence |
| :--- | :--- | :--- |
| Backend ingestion + storage | ✅ implemented | 11 pytest suites |
| Alert / health engine | ✅ implemented | 12 pytest suites |
| Frontend dashboard | ✅ implemented | React + Vite + ECharts; 5 component test files |
| i18n | ✅ EN (default) + KO + ZH | live switcher in header |
| CI | ✅ green | GitHub Actions: backend / frontend / contract gate / real-DB e2e |
| OSS scaffold | ✅ in place | README×3, CONTRIBUTING×3, CoC, SECURITY, issue/PR templates |

## Phase 0 — Polish & welcome (now → ~1 week)

Make the repo instantly approachable and give newcomers clear entry points.

- [x] Public release of the repository (MIT, GitHub).
- [x] Multilingual README (EN/KO/ZH) + CONTRIBUTING (EN/KO/ZH).
- [x] Dashboard preview screenshots in README.
- [ ] Close stale build-epic issues; convert to a tracked milestone.
- [ ] Tag ~10 `good first issue`s across frontend / backend / docs / i18n / a11y.

## Phase 1 — Onboarding (1–2 weeks)

Goal: first external contributions land.

- [x] `CODEOWNERS` + `dependabot.yml` for automated dependency PRs.
- [ ] Verify a one-command full-stack `docker compose` end-to-end.
- [ ] Seed GitHub Discussions (Q&A / Show & tell / Ideas).
- [ ] Add a "How to propose a feature" guide section.

## Phase 2 — Growth (ongoing)

Broader capability and a steady contributor base.

- [ ] Expand frontend test coverage (`FleetHealthAnalytics`, `JointHeatmap`, `FleetOverviewCards`, `HealthSummaryHeader`).
- [ ] Accessibility (a11y) pass + `axe-core` test.
- [ ] More locales (e.g. Japanese).
- [ ] Alert notifier integrations (Slack / Discord / email).
- [ ] Observability: Grafana dashboard example + blackbox probes.
- [ ] Dark mode / theme toggle.

## How to propose

1. Open a **Feature request** issue, or start a **Discussions · Ideas** thread.
2. For scoped work, label it `good first issue` or `help wanted`.
3. For large changes, sketch the design under `docs/` and get a thumbs-up before coding.

## Help right now

See the [`good first issue`](https://github.com/TopRemnantAcademy/robot-monitor/labels/good%20first%20issue) and [`help wanted`](https://github.com/TopRemnantAcademy/robot-monitor/labels/help%20wanted) labels.
