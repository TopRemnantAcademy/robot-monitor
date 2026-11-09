# Contributing to RobotMonitor

Thanks for your interest in improving RobotMonitor! This guide covers how to set up, branch, and open a pull request.

## Code of conduct

By participating you agree to abide by our [Code of Conduct](./CODE_OF_CONDUCT.md).

## Language

- **Discussion & issues:** English is the lingua franca, but Korean and Chinese are welcome (many contributors are on-floor operators in a Korean humanoid factory and the Chinese-diaspora ops community).
- **Code & comments:** write in **English** so the widest contributor base can review.
- **UI strings:** never hard-code display text. Add it to [`frontend_dashboard/src/i18n/locales.ts`](./frontend_dashboard/src/i18n/locales.ts) (English key + Korean + Chinese), and resolve it through `useI18n()`. Server-supplied data (alert messages, robot names) is intentionally not translated.

## Development setup

### Full stack (Docker)

```bash
docker compose -f infra/docker-compose.yml up -d
bash test_integration.sh
```

### Frontend only (no Docker)

```bash
cd frontend_dashboard
npm install
node mock_backend.mjs   # mock WS + REST on :8787
npm run dev             # Vite dev server on :5173
```

See [docs/developer-guide.md](./docs/developer-guide.md) for the full walkthrough, including how the mock backend replaces EMQX/TimescaleDB/Redis for UI work.

## Branching & commits

- Branch from `main`: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Keep commits focused and explanatory.
- **No secrets** in commits. `.env`, `.env.local`, and `*.token` are git-ignored; never force-add them.
- CI must stay green (see [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)): backend pytest, frontend `npm test` + typecheck + `vite build` + bundle-size guard, contract gate, and the real-DB e2e (30k landing).

## Pull requests

- Fill out the [PR template](./.github/PULL_REQUEST_TEMPLATE.md).
- Link the related issue(s).
- Describe what changed and **why**, with evidence (test output, screenshots, before/after) where relevant.
- For UI changes, confirm `npm test` and `npm run typecheck` pass locally.

## Reporting bugs & requesting features

Please use the issue templates under [`.github/ISSUE_TEMPLATE/`](./.github/ISSUE_TEMPLATE/):
- **Bug report** — reproducible steps, expected vs actual, environment.
- **Feature request** — the operator pain point and proposed behavior.
- **Good first issue** — small, well-scoped tasks for newcomers.

## Security

Found a vulnerability? **Do not open a public issue.** Follow [SECURITY.md](./SECURITY.md) and report privately.

## License

By contributing, you agree your contributions are licensed under the [MIT License](./LICENSE).
