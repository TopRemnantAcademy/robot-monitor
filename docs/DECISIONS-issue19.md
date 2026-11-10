# Issue #19 — Three Decisions (2026-10-10)

> User's exact words: *"You decide these for me — give me the plan and the direction."*
> This document is a **decision record + rationale**, not a long analysis. Each decision
> separates **fact** from **judgment**; anything actionable has already been put into code
> (see the "Implementation" section of each item).

---

## Decision Overview

| # | Topic | Decision | Implementation |
|---|---|---|---|
| 1 | Telemetry retention window | **30 days** | `infra/init.sql` §7 (done) |
| 2 | Alert notification channel | **Custom generic webhook forwarder** (DingTalk / WeCom / generic, three modes), **do not adopt keep** | `alert_engine/app/notifiers/` (done) |
| 3 | Dispatch order | **I-17 first, then I-12**; both can run in parallel, I-17 is the stop-bleeding fix | §3 of this document |

---

## Decision 1: Retention window = 30 days

### Facts (capacity estimate, computed column by column, not "a feeling")

Row width is accumulated column by column from `robot_telemetry` in `infra/init.sql`:

| Part | Estimate |
|---|---|
| Scalar columns (time/robot_id/status/error/battery×2/max_joint/pos×3/orient×4) | ~110 B |
| 3 × JSONB (each stores 24 joint floats, ~300 B each after textification) | ~900 B |
| PG row header 24 B + page/index overhead ×1.3 | — |
| **Total** | **≈ 1.34 KB/row** |

```
1000 row/s × 86400 s = 86,400,000 行/天
86,400,000 × 1.34 KB ≈ 116 GB/天（原始）
```

| Retention | Raw | 10x compression | 15x compression |
|---|---:|---:|---:|
| 30 days | 3.5 TB | **≈ 350 GB** | ≈ 230 GB |
| 90 days | 10.4 TB | ≈ 1.0 TB | ≈ 700 GB |

> This is **consistent** with the previous verbal estimate of "≈ 400 GB after 30-day compression"
> (this round adds the column-by-column justification).

### Judgment

Choose **30 days**, for three reasons:

1. **A single machine can hold it.** 350 GB falls within the SSD/HDD capacity of a normal
   factory server; no special storage budget is needed just for the retention period.
2. **Business semantics are sufficient.** The dashboard is for **real-time operations**
   (second-to-minute level); historical needs center on "shift handover review (≤1 day)"
   and "weekly review (≤7 days)". 30 days already covers traceability beyond a week,
   and the continuous aggregate `robot_status_10s` can be given a longer retention for
   long-term trends (its volume is two orders of magnitude smaller).
3. **The marginal benefit of 90 days is not worth it.** The extra 60 days need ~650 GB more
   plus a longer backup window, in exchange for only "being able to dig up the joint torque
   of some robot at some millisecond three months ago" — factories will not use it that way.

### Implementation (already in `infra/init.sql` §7)

- `robot_telemetry`: compress `compress_after=1 day`, retain `drop_after=30 days`
- `robot_alert`: retain **180 days** (compliance/review, and volume is tiny: even 10k/day is
  only in the millions)
- Idempotency uses existence checks against `timescaledb_information.jobs`, **not**
  `if_not_exists` (that parameter is unverified in this repo; rationale is in the `init.sql` comments)

### ⚠️ Unverified

This machine has **no Docker, no PostgreSQL, no timescaledb extension**, so the statements in
`init.sql` §7 have **never actually been executed**. What was done:
- Static check of the PL/pgSQL structure (`DO $$`/`END $$;` pairing, `IF`/`END IF;` pairing,
  bracket balance) → all pass
- The **syntactic form** of `add_*_policy` and `ALTER TABLE ... SET (timescaledb.compress, ...)`
  matches the source-generated form in the `external/timescaledb` library verbatim
  (`compression/sql.py:66-77`, `retention/sql.py:7-9`)
- The view/column names `timescaledb_information.jobs` / `proc_name` / `hypertable_name`
  → verified from `external/timescaledb/retention/sql.py:47-58`

**Still needed**: after `docker compose up`, confirm the policy registered successfully
(`SELECT * FROM timescaledb_information.jobs`).

---

## Decision 2: Alert notification channel = custom webhook, do not use keep

### Facts

1. `keep` has **no** DingTalk/WeCom provider — verified: `keep/keep/providers/` contains
   `slack / teams / telegram / webhook / ntfy` (`grep -i ding|wecom` found nothing).
2. The factory-side bots (DingTalk self-built bot / WeCom group bot) both connect via
   **a single HTTP POST + fixed JSON**; no event-management platform is needed.
3. Adopting `keep` = one more service to operate, back up, and upgrade; but all we need is
   "forward one message".
4. `grafana/oncall` is **archived** (`README.md:3`: archived 2026-03-24), not selectable.

### Judgment

**First write a custom forwarder as "generic webhook = three-mode adapter"; do not adopt keep.**

The reasoning also serves the "interface reserved" consideration — the decision logic lives
here, so a future implementation swap won't require changing the caller:

- DingTalk: `{"msgtype":"markdown","markdown":{"title":...,"text":...}}`
- WeCom: `{"msgtype":"markdown","markdown":{"content":...}}` (**different key name** — wrong key → 400)
- generic: pass raw fields through; let the factory's self-built forwarder do the processing
- Threshold: default `min_severity=3`, only push critical, to **prevent low-battery storms from spamming**
- URL contains a token → **read only from an environment variable** (`ALERT_WEBHOOK_URL`),
  never committed to the repo (AGENTS.md §5)
- URL not configured → the whole chain silently skips (default **off**, to avoid spam from misconfig)

### Implementation (already in `alert_engine/`)

- Added `app/notifiers/webhook.py` (standard-library `urllib`, **zero new dependencies**)
- Hook point: `emit()` in `app/main.py:53-66`, **after `save()`** (persist to DB for audit
  first, then notify)
- Config: `app/config.py:44-51` (`alert_webhook_url/kind/min_severity`)
- Verified: `python -m tests.test_notifier` → **14/14 pass**

### ⚠️ Unverified

- **No real DingTalk/WeCom bot available to test** — the unit test uses a fake `urlopen`
  interceptor, verifying "payload shape + business-code judgment + exceptions not leaked
  outward", **not** real delivery.
- `asyncio.to_thread` inside `emit()` is a **serial await** (each alert waits for the
  notification to return). Current alerts are low-frequency events (a few out of 1000 msg/s),
  so it is acceptable; if alerts become dense in the future, switch to a bounded queue +
  single consumer to avoid `emit` being slowed by network RTT.

---

## Decision 3: Dispatch order — I-17 first, then I-12

### Facts (dependency analysis)

| Ticket | Files touched | Mutually blocking? |
|---|---|---|
| I-17 data lifecycle | `infra/init.sql` | — |
| I-12 observability exporter | new `infra/observability/*.yml` (+ possibly `infra/docker-compose.yml`) | **No file overlap** with I-17 |

`grep` evidence: `infra/init.sql` previously had **zero** `retention`/`compression` hits,
i.e. I-17 is purely additive and does not alter any existing table-structure semantics.

### Judgment

**① I-17 → ② I-12**, and the two **can run in parallel** (no file overlap).

- **I-17 first**: disk is a **ticking time bomb** (116 GB/day, fills in a week) and is the
  **cheapest stop-bleeding fix** (only one SQL file, no runtime component introduced).
  **Stop-bleeding changes must come first.**
- **I-12 follows**: it is "metrics". But note — **metrics serve later changes**; when disk
  is about to fill, bringing up the exporter first only lets you see the disk-full curve earlier.
- **I-12's real constraint**: the exporter must be on the **same network** as `backend`/`redis`/
  `timescaledb`, otherwise `DATA_SOURCE_NAME` won't resolve (a pitfall hit in earlier rounds).
- Order after that (per §7.11): I-15 contract → I-13 auth → I-14 notification
  (**already done early this round**) → I-16 stress test.

> Note: **I-14 was delivered this round alongside Decision 2** (notifier + unit test), no
> longer queued behind I-13's auth — because the notification "recipient" is the group-bot
> URL, unrelated to "who logged into the system", so **no dependency exists**.
> The original plan's line "I-14 depends on I-13's identity" was **overturned by this round's facts**.

---

## Appendix: This Round's Honest Boundaries

- **No Docker / no PostgreSQL** → `init.sql` §7 and the notifier's **end-to-end** were never
  actually run. What did pass: notifier unit test 14/14, SQL structural static check, `compileall`.
- The notifier was **not connected to a real DingTalk/WeCom** (no usable bot available).
- The **compression ratio (10x) for 30 vs 90 days is an industry typical value**, not measured
  in this project — needs real TimescaleDB data load followed by
  `SELECT * FROM hypertable_compression_stats('robot_telemetry')` to confirm.
