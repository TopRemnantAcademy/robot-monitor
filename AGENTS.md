# AGENTS.md — RobotMonitor Global Engineering Discipline (injected at the top of every agent system prompt)

This repository is a **100-humanoid-robot factory monitoring system** (a 2D ops dashboard). The **absolute iron laws** when working in this repo:

## 1. No hallucinations (highest priority)
- Only use dependency versions and APIs pinned in the repo's lock files (`requirements.txt` / `package.json`); **never invent** non-existent library names, class names, function names, API endpoints, or ports.
- Any claim that references a third-party library/API must be traceable to `requirements.txt` / `package.json` or official documentation. If it cannot be traced, mark it "unverified" — never pretend it exists.
- Never use `...` / `// TODO` / `pass` (legitimate empty implementations excepted) as placeholders. Generated files must be complete and runnable.

## 2. Evidence-first (file:line)
- When committing code (PR), the description must include `path:line` evidence for key implementation points.
- Before claiming "done / passed", you must actually run verification (simulator / type check / unit tests) and attach the command and output summary.
- For items that cannot be verified locally, list them in a separate "Unverified" section explaining what is still needed.

## 3. Performance guardrails (prevent 1000 Hz overload)
- Backend: **never INSERT row-by-row**; must use `asyncio.Queue` batch writes (double-trigger on 500 rows OR 1 second) into TimescaleDB.
- Frontend: **never trigger a React re-render on every WS message**; must throttle at 100 ms (useRef buffer + setInterval batch flush to the store).
- Any throughput-related change must first compute the peak (100 robots × 10 Hz = 1000 msg/s) before acting.

## 4. Data contract
- Robot payload fields follow the telemetry Schema defined in `backend_ingestion/app/schemas/telemetry.py` and `simulator/mock_fleet.py` (`robot_id` prefix `humanoid-`, includes the `max_joint_temp` precomputed column).
- The database Schema follows `infra/init.sql`; any Schema change must update `init.sql` and the corresponding ORM model in sync.

## 5. Security
- **Never bake `.env` / secrets / tokens into Docker images or commit them to the repo**; the repo root must have a `.dockerignore` that excludes `.env`.
- Never hardcode production credentials in code.

## 6. Workflow
- Module construction must follow these **six phases** in order, without skipping:
  (infrastructure → simulator → backend → alert engine → frontend → integration; the simulator must produce output before the backend, otherwise the backend cannot be accepted).
- After each module is complete, you must run the end-to-end chain with `simulator/mock_fleet.py`.
- Distinguish "facts" from "judgments"; judgments must state their basis.

## 7. Commits & collaboration
- Do not force-push `main` directly; submit via PR and wait for the reviewer role (review engineer) to approve.
- Communicate in English; keep technical identifiers (file/class/endpoint names) in their original English form.
