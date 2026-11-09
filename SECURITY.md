# Security Policy

## Supported Versions

Security fixes are applied to the latest `main` branch. We do not maintain separate release branches.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately via one of the following:

- GitHub's private vulnerability reporting (recommended): open a **Security Advisory** draft from the repository's "Security" tab.
- Email the maintainers (see the repository owner profile) with the subject `RobotMonitor security report`.

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected component(s) and version/commit.
- Any suggested mitigation, if known.

We will acknowledge receipt within a few business days, keep you informed of progress, and coordinate disclosure once a fix is available.

## Security Notes for Contributors

- **No secrets in the repo.** `.env`, `.env.local`, and token files are git-ignored. CI uses only placeholder secrets (`JWT_SECRET=ci-*-placeholder`); never commit real credentials.
- **Input validation:** the ingestion service accepts untrusted telemetry over MQTT and alerts over WS — treat all external input as hostile (size limits, schema validation, no `eval`/template injection).
- **Auth:** the dashboard and REST APIs require a JWT for mutating operations (e.g. alert acknowledge). Do not weaken the auth gate on `/alerts/{id}/ack` or similar.
- **Dependency hygiene:** keep `requirements.txt` and `package-lock.json` up to date; enable Dependabot/Renovate alerts.
