# Enterprise Capability Gap Analysis

> Trigger: issue #19 comment "this is far from enough now, what does a large enterprise need… both frontend and backend need perfect handoff"
> Method: **only write gaps whose evidence can be found in this repo at `file:line`**; each item gives "current-state evidence → enterprise requirement → directly applicable OSS → dispatch order".
> Discipline: distinguish [Fact] (code already checked) from [Judgment] (my assessment, with basis). Unverified items marked "unverified".

---

## 0. One-Sentence Conclusion

The current system is a **runnable single-node PoC** (the 100-robot × 10Hz chain is closed), but it still needs **4 categories, 17 items** of capability to reach **enterprise-deliverable**.
Among them, 6 are "half-finished" — the database/frontend already buried half the pit, but the other half is not wired up; this class is the most dangerous.

---

## 1. Verified "Half-Finished" Items (highest priority, because 50% done is more misleading)

| # | Phenomenon | Evidence (file:line) | Enterprise Consequence |
|---|---|---|---|
| H1 | `robot_alert.ack` column + partial index built (but the table has no primary key/ID, see H7), yet **no code in the whole repo writes/updates ack**, and no ack button on the frontend | `infra/init.sql:62` (`ack BOOLEAN DEFAULT FALSE`)<br>`infra/init.sql:92` (`idx_alert_ack ... WHERE ack = FALSE`) | Operators cannot "claim/close" alerts → alerts are forever a stream, no SLA closure |
| H2 | Alert `severity` split into 2/3 levels, **but no escalation policy**: stays at original level, no "escalate if unacked for 5 min" | `alert_engine/app/rules/*.py` (each rule only returns severity constant) | Critical alerts drowned out, no response, no fallback |
| H3 | `robot_metadata` table built (100-robot seeding), **but no business code reads it for grouping/owner/station** | `infra/init.sql:33` | Cannot filter by production line/station/shift; 100 robots can only be viewed together |
| H4 | Frontend `HealthSummaryHeader` denominator **hardcoded 100** | `HealthSummaryHeader.tsx:36` (comment: online rate = online / 100)<br>`robotStore.ts:17` (`buildRobotIds(count = 100)`) | Scaling to 200 robots needs code change, not configuration |
| H5 | Frontend `AlertLogTable` only has 200-entry in-memory buffer, **no pagination / no history query** | `robotStore.ts:14` (`ALERT_BUFFER_LIMIT = 200`) | Alert lost at shift handover, no traceability |
| H6 | `RobotDetailDrawer` history fixed at `minutes=5`, **no time-range selector** | `backend_ingestion/app/main.py:84` (`minutes: int = 5`) | Cannot query "what happened yesterday at 14:00" |
| H7 | `robot_alert` **has no primary key, no ID column**, cannot address a single alert | `infra/init.sql:55-64` (columns: `time/robot_id/rule_id/severity/message/detail/ack`); compare `init.sql:27` telemetry table has `PRIMARY KEY (time, robot_id)` | Alerts **cannot be acknowledged/closed** — there is no "which one" handle to ack |

> My judgment (with basis): H1/H3 are typical "schema-first, capability-not-wired". Most feared during enterprise procurement — demo passes, delivery exposes the gap.

---

## 2. Backend Gaps

### 2.1 Security and Identity — Currently **Completely Absent**

**Fact**: Searching the whole repo for `auth / jwt / rbac / token`, `backend_ingestion/app/` and `alert_engine/app/` return **zero hits**. `GET /robots`, `WS /ws/robots`, `GET /robots/{id}/history` are all **bare** (`backend_ingestion/app/main.py:78/84/168`).

Enterprise requirements:
- Authentication (login)
- Authorization (RBAC: operator/shift-lead/admin — what they can see, what they can ack)
- WS auth (carry token on connect, not relying on "internal network" as security)
- Audit (who acked which alert when)

**Directly applicable OSS (verified clonable, see §5)**:

| Source | Purpose | Integration Point |
|---|---|---|
| `fastapi-users/fastapi-users` | FastAPI-native user/login/JWT, **same stack as existing FastAPI** | Add `backend_ingestion/app/auth/`, protect REST + WS |
| `casdoor/casdoor` | Enterprise SSO/OIDC all-in-one (with UI) | If required to integrate with company AD/LDAP |
| `keycloak/keycloak` | Veteran IAM, OIDC/SAML/LDAP | Standard answer for large enterprises |

> Judgment: **use fastapi-users first** (same stack, minimal change, truly runnable), keep the OIDC interface for future Keycloak swap. Don't cram Keycloak in right away — that's using a cannon to kill a mosquito.

### 2.2 REST/WS Contract Not Machine-Enforced

**Fact**: REST has `?minutes=5&resolution=10s` (`main.py:83-84`), WS has `type` envelope (`main.py:176/193/195`), but **no OpenAPI contract snapshot, and no frontend type auto-generation**. Front and back align by hand — already caused an incident (`STATUS.md` §6 item 2: `/history` missing `joint_positions` caused the Drawer's third line chart to stay empty).

Enterprise requirement: **contract as code**, with breaking-change detection in CI.

**Applicable**:
| Source | Purpose |
|---|---|
| `OpenAPITools/openapi-generator` | Generate TS types from FastAPI's `/openapi.json` → frontend `types/robot.ts` no longer hand-written |
| `open-telemetry` (see 2.4) | Contract/trace together |

### 2.3 Alert Capability Is Only "Rule Judgment", Lacks Ops Closure

**Fact**: 4 rules + dedup (`alert_engine/app/rules/`, `deduplicator.py`), Order 10 is adding **inhibition**. But still missing:

| What's missing | Why enterprise needs it | Applicable |
|---|---|---|
| Acknowledge/close (runbook) | Alerts must be "handled", otherwise forever noisy | Custom + H1's `ack` column |
| Escalation | Auto-escalate/notify supervisor if unacked for 5 min | Alertmanager's `route`/`repeat_interval` semantics |
| Silence | Stay quiet during planned maintenance | Alertmanager `silence` |
| Notification channels | Beyond the big screen, also WeCom/DingTalk/email | `samber/awesome-prometheus-alerts` (rule template library) |
| Notification debounce/merge | One incident shouldn't send 100 messages | Alertmanager `group_by` |

**Applicable**: `prometheus/alertmanager` (already in `external/`, sparse docs) — fully implement the `inhibit/silence/route` trio by comparison, not just copy inhibition.

### 2.4 Observability — Currently **Zero**

**Fact**: No metrics endpoint, no structured logs, no trace in the whole repo. `GET /healthz` (`main.py:66`) only returns liveness, not "queue backlog / persist p99".

Enterprise requirement (**the physical basis of "perfect front-back handoff"**):
- Backend: queue water level, persist throughput, Redis round-trip, WS connection count
- Frontend: **React re-render frame rate, WS message rate, throttle hit rate** (AGENTS.md §3's performance guardrails are currently **unverifiable**)
- Full chain: MQTT→backend→DB end-to-end latency

**Applicable**:
| Source | Purpose | This Project's Existing Basis |
|---|---|---|
| `prometheus/client_python` | Backend `/metrics` | Already downloaded (`INTEGRATION-PLAN.md` I-4) |
| `open-telemetry/opentelemetry-python` | Backend trace | Can directly hook FastAPI middleware |
| `open-telemetry/opentelemetry-js` | **Frontend trace + re-render observability** | Frontend zero basis, core of this order |
| `openobserve/openobserve` / `grafana/loki` | Log aggregation (self-hosted, lightweight) | Current logs only go to stdout |

> Judgment: Frontend observability is **the most neglected layer**. AGENTS.md explicitly forbids "every WS message triggering a re-render", but **no code proves it doesn't happen**. Enterprise acceptance will directly ask this.

### 2.5 Data Layer

**Fact**:
- Hypertable + continuous aggregates built (`infra/init.sql:30/65/70`)
- **No retention policy**: telemetry accumulates at 1000 rows/sec, no TTL will fill the disk
- **No compression policy**: TimescaleDB's killer feature unused
- **No migration tool**: schema changes by hand-editing `init.sql`
- `asyncpg==0.29.0` still pinned old version (`backend_ingestion/requirements.txt:5`, Order 08 in progress)

**Applicable**: `sqlalchemy/alembic` (already downloaded); retention/compression policy is built-in TimescaleDB SQL, no external source needed.

---

## 3. Frontend Gaps

### 3.1 Only "One Big Screen", No Enterprise Frontend Skeleton

**Fact**: `frontend_dashboard/src/` has 13 files total, no routing (no `react-router`), no login page, no permission control, no layout framework. `App.tsx` directly stacks three components vertically.

Enterprise requirement:
| Capability | Current | Applicable |
|---|---|---|
| Routing/multi-page (big screen / alert center / robot profile / reports) | ❌ single-page stack | `remix-run/react-router` or `tanstack/router` |
| Server state cache (history query / pagination / retry) | ❌ bare `fetch` | `tanstack/query` |
| Forms and validation (threshold config, robot metadata) | ❌ none | `react-hook-form` |
| Component library (table / date picker / modal / dropdown) | ❌ all hand-written | `shadcn-ui` or `ant-design` (**antd is more convenient for industrial dashboards**) |
| Internationalization (common in Sino-foreign joint-venture factories) | ❌ hardcoded Chinese | `i18next` |

### 3.2 Specific Interfaces Missing for "Perfect Handoff"

Mapping backend capability to frontend, **each backend gap corresponds to a frontend gap**:

| Backend capability | Frontend should have | Current |
|---|---|---|
| RBAC | Hide/disable buttons by role | ❌ |
| `ack` column (H1) | Alert "ack/claim" button + acknowledged state | ❌ |
| Escalation policy (H2) | Escalation marker + prominent color | ❌ |
| `robot_metadata` (H3) | Filter by production line/station/shift | ❌ |
| Arbitrary history interval (H6) | Time selector + zoom | ❌ |
| Alert history (H5) | Paginated table + export | ❌ |
| Retention policy | "Data kept only N days" hint | ❌ |
| Observability | Big screen's own health (WS rate / disconnect) | Only WS red/green dot (`HealthSummaryHeader`) |

### 3.3 Frontend Quality Engineering — Has Directory But Not Systematized

**Fact**: `package.json` has `puppeteer`/`jsdom`, `orders/09` mentions `.e2e/`, but **no unit test files under `src/`**, and `package.json` has no `test` script.

Enterprise requirement: unit tests + component tests + E2E + visual regression (big screens most fear "change one place, break a patch"). **Applicable**:
| Source | Purpose |
|---|---|
| `testing-library/react-testing-library` | Component tests |
| `mswjs/msw` | Mock WS/REST (**essential for offline frontend dev**) |
| `microsoft/playwright` | E2E + screenshot comparison (better than puppeteer for regression) |
| `vitest-dev/vitest` | Test runner same stack as Vite |

---

## 4. Delivery and Ops (must-check for enterprise procurement)

**Fact**:
- `infra/docker-compose.yml` has 3 services + healthcheck, but **no resource limits (`deploy.limits`), no restart policy**
- No CI deployment artifacts, no environment tiers (dev/staging/prod)
- Single node, no HA

| What's missing | Enterprise requirement | Applicable |
|---|---|---|
| Container orchestration | K8s/Helm or at least compose prod profile | `helm/helm` (if needed) |
| Reverse proxy + TLS | Nginx/Traefik + HTTPS | `traefik/traefik` |
| Secret management | Not plaintext `.env`, use Vault/K8s Secret | `hashicorp/vault` |
| Backup/recovery | TimescaleDB periodic dump + PITR | Built-in DB capability |
| On-call/notification | Integrate with WeCom/DingTalk/Slack | Alertmanager receiver |

---

## 5. Downloadable OSS List (**all verified reachable via `git ls-remote`**)

> Verification time: this round. Command: `git ls-remote --heads https://github.com/<repo>.git`, each returned refs = reachable.

| # | Repo | Reachable | Corresponding gap | Priority |
|---|---|---|---|---|
| E1 | `fastapi-users/fastapi-users` | ✅ | 2.1 auth/authorization | **P0** |
| E2 | `open-telemetry/opentelemetry-python` | ✅ | 2.4 backend observability | **P0** |
| E3 | `open-telemetry/opentelemetry-js` | ✅ | 2.4 frontend observability | **P0** |
| E4 | `prometheus/alertmanager` | ✅ (already present) | 2.3 silence/route/escalation | **P0** |
| E5 | `samber/awesome-prometheus-alerts` | ✅ | 2.3 rule template library | P1 |
| E6 | `OpenAPITools/openapi-generator` | ✅ | 2.2 contract as code | **P0** |
| E7 | `openobserve/openobserve` | ✅ | 2.4 log aggregation (lightweight self-hosted) | P1 |
| E8 | `grafana/loki` | ✅ | 2.4 log aggregation (alternative) | P2 |
| E9 | `casdoor/casdoor` | ✅ | 2.1 enterprise SSO | P2 |
| E10 | `keycloak/keycloak` | ✅ | 2.1 enterprise IAM | P2 |
| E11 | `microsoft/playwright` | ✅ | 3.3 E2E + visual regression | P1 |
| E12 | `tanstack/query` | ✅ | 3.1 server state | P1 |
| E13 | `mswjs/msw` | ✅ | 3.3 mock | P1 |
| E14 | `prometheus/blackbox_exporter` | ✅ | 4 external availability probe | P2 |
| E15 | `victoriametrics/victoria-metrics` | ❌ not reachable this time | — (downgrade: use Prometheus itself) | — |
| E16 | `apache/apisix` | ✅ | 4 gateway/rate-limit | P2 |
| E17 | `temporalio/temporal` | ✅ | 2.3 alert workflow (heavy reorchestration use) | P3 |
| E18 | `camunda/camunda` | ✅ | 4 maintenance work-order flow | P3 |

> **Honest disclosure**: `victoriametrics/victoria-metrics`'s `ls-remote` failed this time (possibly transient network), its reachability unverified, so it is not listed as recommended. The other 17 all returned refs in this round's verification.

---

## 6. Dispatch Suggestions (by priority, directly executable)

| Order | Name | Corresponding gap | Criterion (verifiable) |
|---|---|---|---|
| 11 | **Auth and RBAC rollout** | 2.1 / 3.2 | `fastapi-users` integrated; `/robots` without token returns 401; WS connection auth; operator/admin two roles |
| 12 | **Observability three-piece** | 2.4 / 3.2 | Backend `/metrics` (queue water level / persist p99); frontend reports re-render count and WS rate; OTel trace through MQTT→DB |
| 13 | **Alert closure** (ack/escalation/silence) | H1/H2/**H7**/2.3 | First add `robot_alert` primary key/ID column (blocker); `ack` truly written; frontend ack button; 5-min unacked escalation; silence effective |
| 14 | **Contract as code** | 2.2 | CI generates TS types; `/history` field change triggers type error |
| 15 | **Enterprise frontend skeleton** | 3.1 / H3/H5/H6 | 4 routed pages; filter by production line; alert pagination/export; history time selector |
| 16 | **Data lifecycle** | 2.5 | retention + compression policy rolled out; alembic migration baseline |
| 17 | **Frontend quality engineering** | 3.3 | vitest unit + playwright E2E + visual regression screenshot baseline |

---

## 7. My Judgments (not facts, with basis)

1. **The biggest risk is not "few features" but "many half-finished items"** (the 6 in §1). Basis: `ack` column + index built but no code; `robot_metadata` seeded but no consumer. Things that "look present" are more fatal in enterprise acceptance than things "clearly absent".
2. **The key to "perfect front-back handoff" is the observability in §2.4, not adding more pages.** Basis: AGENTS.md §3 sets a hard performance guardrail (100ms throttle), but currently has **no means to verify** — equivalent to no guardrail. "Handoff" without observability is a slogan.
3. **Do auth ourselves (fastapi-users), keep IAM heavyweights as interface, don't rush to adopt.** Basis: this project is a single-plant scenario with 100 robots; Keycloak's ops cost far exceeds the benefit; but if the client is a large enterprise group, SSO is an admission condition, so **the interface must be reserved**.
4. **Frontend should move from "hand-write everything" to "component library + state cache" as soon as possible.** Basis: `src/` already has only 13 files yet produced cross-end contract incidents like "Drawer's third line chart stays empty"; at larger scale, hand-write cost and defect rate rise non-linearly.
5. **Order 13's hidden blocker is table structure, not business logic.** Basis: `robot_alert` has no primary key and no ID (`infra/init.sql:55-64`), yet its `ack` column and partial index are already built — showing the design thought about it but **stopped halfway**. Such "half-finished" items must be prioritized, because they are directly exposed during demos.

---

## 8. Unverified List (honest disclosure)

| Item | Reason | What's needed to verify |
|---|---|---|
| Specific file paths/versions of the above 17 repos | This round only did `ls-remote` reachability, no clone of content | `git clone --depth 1` then check |
| `fastapi-users` compatibility with FastAPI 0.110 | Not installed/tested | Local venv actual install |
| OTel JS overhead in frontend 100ms throttle scenario | Not stress-tested | Needs real build + stress test |
| Impact of TimescaleDB retention/compression on this project's chunks | Not executed on real DB | Needs Docker to bring up TimescaleDB |
| Frontend visual regression baseline | playwright not run | Needs browser environment |

---

*All file:line in this document can be checked line by line; unverified items are explicitly marked in §8.*
