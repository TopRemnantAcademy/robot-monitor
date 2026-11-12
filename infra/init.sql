-- robot_monitor init schema for TimescaleDB (PostgreSQL 16 + timescaledb 2.13)
-- This file is executed automatically by /docker-entrypoint-initdb.d/ on the first start of the timescaledb container.

-- 1. Enable the timescaledb extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 2. High-frequency telemetry table
-- Note: the composite primary key must include the partitioning column time (a hard TimescaleDB requirement), hence (time, robot_id).
CREATE TABLE IF NOT EXISTS robot_telemetry (
    time              TIMESTAMPTZ     NOT NULL,
    robot_id          TEXT            NOT NULL,
    status_code       INTEGER         NOT NULL,   -- 0 offline / 1 normal / 2 warning / 3 fault
    error_code        INTEGER         NOT NULL DEFAULT 0,
    battery_pct       DOUBLE PRECISION,
    battery_temp      DOUBLE PRECISION,
    max_joint_temp    DOUBLE PRECISION,  -- Precomputed: highest temperature across all joints (public/external data source may be null)
    joint_temperatures JSONB,
    joint_positions     JSONB,
    joint_torques       JSONB,
    pos_x DOUBLE PRECISION,
    pos_y DOUBLE PRECISION,
    pos_z DOUBLE PRECISION,
    orient_w DOUBLE PRECISION,
    orient_x DOUBLE PRECISION,
    orient_y DOUBLE PRECISION,
    orient_z DOUBLE PRECISION,
    PRIMARY KEY (time, robot_id)
);

SELECT create_hypertable('robot_telemetry', 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

-- 2.1 Policy action table (R-VLA-EVAL Phase 1)
--
-- A *parallel* high-frequency table for VLA policy action frames. It is
-- intentionally decoupled from `robot_telemetry` (different consumer: the
-- evaluation/observability base, not the fleet-health dashboard) so the
-- existing telemetry contract and the 38w+ historical rows are never affected.
--
-- Identity semantics mirror robot_telemetry: the composite PK must include the
-- partitioning column `time` (a hard TimescaleDB requirement), hence (time, robot_id).
-- `policy_version` ties each row to a trained checkpoint; `trial_id` ties it to an
-- A/B evaluation trial (Phase 2+) and is nullable for normal operation.
-- `action` is the raw action vector (JSONB to stay schema-flexible across
--   policies of differing dimensionality — 6-DoF up to 64-D joint targets).
-- `goal` is the natural-language instruction the policy was conditioned on
--   (nullable; may be large, so it is NOT indexed).
CREATE TABLE IF NOT EXISTS policy_actions (
    time              TIMESTAMPTZ     NOT NULL,
    robot_id          TEXT            NOT NULL,
    policy_version    TEXT            NOT NULL,
    trial_id          TEXT,
    action            JSONB           NOT NULL,
    goal              TEXT,
    PRIMARY KEY (time, robot_id)
);

SELECT create_hypertable('policy_actions', 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

-- 6.4 policy_actions: also a hypertable; low-frequency relative to telemetry but
--     equally needs compression + retention guardrails (mirrors §6.2 telemetry 30d).
ALTER TABLE policy_actions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'robot_id',
    timescaledb.compress_orderby   = 'time DESC'
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression'
          AND hypertable_name = 'policy_actions'
    ) THEN
        PERFORM add_compression_policy('policy_actions', INTERVAL '1 day');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_retention'
          AND hypertable_name = 'policy_actions'
    ) THEN
        PERFORM add_retention_policy('policy_actions', INTERVAL '30 days');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_policy_actions_robot_time ON policy_actions (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_policy_actions_trial ON policy_actions (trial_id) WHERE trial_id IS NOT NULL;

-- 3. Robot metadata table (low frequency)
CREATE TABLE IF NOT EXISTS robot_metadata (
    robot_id         TEXT PRIMARY KEY,
    model_type       TEXT,
    firmware_version TEXT,
    assigned_station TEXT,
    is_online        BOOLEAN DEFAULT FALSE,
    last_seen        TIMESTAMPTZ
);

-- 3.1 Preseed 100 robots (humanoid-001 ~ humanoid-100)
-- Purpose: the dashboard can render 100 cards even when the system has no data yet; the backend WS snapshot can enumerate IDs from this;
--       the denominator for online-rate is fixed at 100, not dependent on "number of robots that have reported data".
INSERT INTO robot_metadata (robot_id, model_type, firmware_version, assigned_station)
SELECT
    'humanoid-' || lpad(g::text, 3, '0'),
    'HUMANOID-24DOF',
    '1.0.0',
    'STATION-' || lpad(((g - 1) % 10 + 1)::text, 2, '0')
FROM generate_series(1, 100) AS g
ON CONFLICT (robot_id) DO NOTHING;

-- 4. Alert table
--
-- Identity semantics (order 19): `alert_id BIGSERIAL` + `PRIMARY KEY (time, alert_id)`.
--   - The primary key must include the partitioning column time (a hard TimescaleDB requirement), hence the order is (time, alert_id).
--   - alert_id is monotonically increasing and uniquely identifies "which alert for the same robot at the same instant":
--     within one tick of alert_engine, motor_temp / battery_low / joint_stuck each have an independent
--     deduplication window (DEDUP_WINDOW in alert_engine/app/main.py); the same (time, robot_id)
--     may legitimately map to multiple alerts from different rules -- the old semantics "(time, robot_id) is identity" is wrong.
--   - ack_at / ack_by record "acknowledged" against a specific alert and capture the ack time/person;
--     this round only defines the columns; the ack API belongs to order 17 (alert closed-loop).
--
-- ⚠️ Migration of existing databases is out of scope for this change: this file is an **initialization** script (run on first start by
--    /docker-entrypoint-initdb.d/), and will not recreate tables for an already-existing database.
--    For a database with existing data, run manually:
--      ALTER TABLE robot_alert ADD COLUMN alert_id BIGSERIAL;
--      ALTER TABLE robot_alert ADD COLUMN ack_at TIMESTAMPTZ;
--      ALTER TABLE robot_alert ADD COLUMN ack_by TEXT;
--      ALTER TABLE robot_alert ADD PRIMARY KEY (time, alert_id);
--    (the above statements on the TimescaleDB side were not verified on this machine; see the notes at the end of this file.)
--
--    Order 17 added the escalated (escalation flag) column; for existing databases, apply the migration:
--      ALTER TABLE robot_alert ADD COLUMN escalated BOOLEAN NOT NULL DEFAULT FALSE;
--    This statement was verified on a plain PG15.19 locally (see the "verification" section of the PR); ack_by/ack_at
--    were added by order 19, so this change does **not** repeat ADD COLUMN.
--
--    Order 35 added suppressed / suppressed_by (suppression flags, threaded through the full stack):
--      ALTER TABLE robot_alert ADD COLUMN suppressed BOOLEAN NOT NULL DEFAULT FALSE;
--      ALTER TABLE robot_alert ADD COLUMN suppressed_by TEXT;
--    Background: order 30 implemented suppression "determination and logging" (InhibitionRecord + WARNING log +
--    robot_alert_suppressed_total metric), but the suppressed alert was **not written to the database**,
--    so `GET /alerts` could never find it -- operators could only grep the logs.
--    This change also persists "the suppressed one" and sets suppressed=TRUE, making suppression queryable in the REST contract and
--    flaggable as "suppressed" in the frontend. **The suppression determination semantics are unchanged** (order 30's hard constraint
--    of "do not silently discard" is actually only closed by this step: `InhibitionRecord` was never persisted on the production path).
--    For existing-database migration, the two ALTER statements above suffice (DEFAULT FALSE keeps the semantics of historical rows unchanged).
CREATE TABLE IF NOT EXISTS robot_alert (
    time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- alert_id follows the partitioning column time: column order matches the ORM declaration order and the PK order, for easy column-by-column comparison.
    alert_id  BIGSERIAL NOT NULL,
    robot_id  TEXT        NOT NULL,
    rule_id   TEXT        NOT NULL,   -- motor_temp | battery_low | offline | joint_stuck
    severity  INTEGER     NOT NULL,   -- 1 info / 2 warning / 3 critical
    message   TEXT        NOT NULL,
    detail    JSONB,
    ack       BOOLEAN     NOT NULL DEFAULT FALSE,
    ack_at    TIMESTAMPTZ,
    ack_by    TEXT,
    -- escalated: set TRUE when severity=3 and it times out without being acked (order 17 escalation closed-loop).
    -- Written by the ticker in alert_engine/app/escalation.py; the frontend shows "⚠ escalated" based on it.
    escalated BOOLEAN     NOT NULL DEFAULT FALSE,
    -- suppressed: this alert is **suppressed** (order 35). Order 30's suppress_record writes it together with
    -- its traceable evidence; previously it only wrote logs and metrics, not the database, so it was unqueryable in the REST contract.
    -- Semantics: TRUE = determined to be suppressed (no WS push, no notification); but the **row is retained**, queryable and auditable.
    suppressed    BOOLEAN NOT NULL DEFAULT FALSE,
    -- suppressed_by: the **root-cause rule** that suppressed it (e.g. offline / motor_temp).
    -- Paired with suppressed: must be non-null when set TRUE. TEXT rather than a foreign key -- rules are code assets
    -- (the rule_id in app/rules/*.py), with no corresponding rule table to reference.
    -- Why not also persist the source severity / reason as columns: that is the job of the `detail` JSONB (no schema-drift risk).
    suppressed_by TEXT,
    -- The primary key must include the partitioning column time (a hard TimescaleDB requirement)
    PRIMARY KEY (time, alert_id)
);

SELECT create_hypertable('robot_alert', 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

-- 5. Continuous aggregate: latest 10-second snapshot per robot (for dashboard / history charts at second-level refresh)
--
-- ⚠️ The materialized_only footgun (R-16, same root as R-13; verified on a real DB at 2.13.1):
--   This view serves the **dashboard's latest state** (i.e. the "current status/battery/temperature of each robot" in `/robots`),
--   and must use **real-time aggregation** (`materialized_only` set to `false`), otherwise the dashboard's latest values have a **1min gap**.
--
--   ① The default is `true`, not `false`: TimescaleDB 2.13.1 source
--      `src/ts_catalog/continuous_agg.c:58-62`
--        `[ContinuousViewOptionMaterializedOnly] = { .arg_name = "materialized_only",
--                                                   .default_val = (Datum)true };`
--      ⇒ This view is **created with `materialized_only = true`** (real DB
--      `_timescaledb_catalog.continuous_agg` measured: all three caggs are `t`);
--      `WITH NO DATA` does not change this default; only explicitly writing `materialized_only = false` flips it.
--   ② Consequence of the default true: the query view is a **pure materialized table** (`pg_get_viewdef` has no
--      `UNION ALL ... FROM robot_telemetry` real-time segment), and materialization only covers up to `now() - end_offset`,
--      the `[now-end_offset, now]` interval is **unqueryable**. This view has `end_offset = 1 minute`
--      (see the add_continuous_aggregate_policy below), so the dashboard's latest state lacks the most recent 1 minute
--      (compounded by `schedule_interval = 1 minute`, even more stale) -- same root as the `/history`
--      right-end gap that R-13 fixed, only the consumer and `end_offset` differ.
--   ③ Fix: this requirement uses §5.1d
--      `ALTER MATERIALIZED VIEW IF EXISTS robot_status_10s SET (timescaledb.materialized_only = false);`
--      to enable real-time aggregation (idempotent, re-running `init.sql` is safe: re-executing on a view already at that value is a no-op).
--      **Do not** set it back to `true` -- that is exactly this bug itself; the regression test
--      `tests/e2e/test_cagg_realtime_backfill.py` will immediately turn red on a real DB.
--   ④ `end_offset = 1 minute` only defines the **materialization boundary**, and does not change: the right-side real-time segment is transparently backfilled by the engine against
--      `robot_telemetry`, and the query SQL and column set are **byte-for-byte unchanged**.
--      This requirement does not change the refresh policy (the three policy parameters below keep their original values), does not change the column set
--      (status_code/battery_pct/max_joint_temp/error_code), and does not change the frontend.
--
-- Verified: TimescaleDB 2.7+ continuous aggregates support first()/last() (explicitly shown in the official docs),
-- this repo pins 2.13.1, so last(...) is available and no rewrite to max() is needed.
CREATE MATERIALIZED VIEW IF NOT EXISTS robot_status_10s
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('10 seconds', time) AS bucket,
    robot_id,
    last(status_code, time)  AS status_code,
    last(battery_pct, time)  AS battery_pct,
    max(max_joint_temp)      AS max_joint_temp,
    last(error_code, time)   AS error_code
FROM robot_telemetry
GROUP BY bucket, robot_id
WITH NO DATA;

-- Refresh policy (R-16 **must not be changed**: all three parameters keep their original values): end_offset=1min / schedule=1min.
SELECT add_continuous_aggregate_policy('robot_status_10s',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);

-- 5.1 Continuous aggregate (ISSUE #76): dedicated to `/history`, **includes JSONB joint columns**
--
-- Why a new aggregate is required instead of extending robot_status_10s:
--   ① `/history` needs the three columns joint_positions / joint_temperatures / joint_torques
--      (the data source for the Drawer's three line charts and the heatmap), while robot_status_10s only has scalar columns;
--   ② robot_status_10s is already asserted by R-08 §A4 (view exists), test_integration.sh:172,
--      and tests/e2e/test_e2e_30k.py in three places; changing columns would undermine these existing acceptance checks;
--   ③ the two aggregates have **different consumers and refresh semantics**: robot_status_10s serves "dashboard latest state",
--      while this view serves "single-robot history replay". Merging would make their retention/refresh policies interfere with each other.
--
-- Why JSONB can enter a continuous aggregate (source-verified, not speculation; this machine pulled timescaledb 2.13.1
-- source and cross-checked it; see the N21 entry in external/SOURCES.md for the path):
--   ① The first parameter of `last(value, time)` is **polymorphic anyelement**
--      (the `ts_last_sfunc` signature at timescaledb `src/agg_bookend.c:433`
--      `last(internal, anyelement, "any")`), JSONB and INTEGER take the same path,
--      there is no "JSONB does not support aggregation" limitation; the "missing column" mentioned in the old comment was simply **not written** at the time, not that it couldn't be.
--   ② internally partialize goes through `_timescaledb_functions.partialize_agg(arg ANYELEMENT)`
--      (`sql/partialize_finalize.sql:6-7`), also polymorphic, so JSONB can be materialized directly.
--   ③ No hidden column-order requirement: the continuous aggregate's **partitioning column** is determined by the TargetEntry where `time_bucket` sits
--      in the targetlist (`tsl/src/continuous_aggs/finalize.c:875-877`
--      sets `matpartcolno/matpartcolname`), and does not require it to be first;
--      `caggtimebucket_validate` only validates "exactly one time_bucket, applied to the partitioning column"
--      (`tsl/src/continuous_aggs/common.c:152-206`), and does not validate position.
--      Below we still put bucket first -- it reads most intuitively and stays isomorphic with robot_status_10s.
--
-- The key names and column names match robot_status_10s's existing columns verbatim, plus the three joint columns;
-- the column names are exactly the field names in `/history`'s response body; drift on either side will directly blank out the Drawer.
--
-- ⚠️ The materialized_only footgun (R-13, verified on a real DB at 2.13.1): this view must use **real-time aggregation**
--   (`materialized_only` set to `false`), otherwise the `/history` short-span path has a 10s gap at its right end.
--   TimescaleDB **2.13.1 continuous aggregates default `materialized_only` to `true`** -- source
--   `src/ts_catalog/continuous_agg.c:58-62`:
--     `[ContinuousViewOptionMaterializedOnly] = { .arg_name = "materialized_only",
--                                                .default_val = (Datum)true };`
--   The default is **`true`**; only explicitly writing `materialized_only` as `false` when creating the view flips it
--   (`WITH NO DATA` does not change this default). The old comment's "2.x defaults to false / real-time aggregation by default" is **wrong**;
--   the real DB's `_timescaledb_catalog.continuous_agg` measured: all three caggs (including this view) are created with `materialized_only = t`.
--   Consequence of default true: the materialized table only covers up to `now() - end_offset`, and `[now-end_offset, now]` is excluded from queries
--   (`pg_get_viewdef` is a pure materialized-table SELECT, with no `UNION ALL ... FROM robot_telemetry` real-time segment).
--   ⇒ This requirement uses §5.1c `ALTER MATERIALIZED VIEW ... SET (timescaledb.materialized_only = false)`
--      to switch back to real-time aggregation (idempotent, re-running init.sql is safe); **do not** set it to `true` (that is exactly this bug itself).
--   `end_offset` (lines :198-202) only defines the **materialization boundary**; the right-side real-time segment is transparently backfilled by the engine against `robot_telemetry`,
--   without changing the refresh policy, the columns, or the `/history` SQL.
CREATE MATERIALIZED VIEW IF NOT EXISTS robot_history_10s
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('10 seconds', time) AS bucket,
    robot_id,
    last(status_code, time)        AS status_code,
    last(battery_pct, time)        AS battery_pct,
    max(max_joint_temp)            AS max_joint_temp,
    last(error_code, time)         AS error_code,
    -- ↓ The three JSONB joint columns: the core of ISSUE #76, and the flipside lesson of order 16's "missing column -> permanently empty chart".
    --   joint_positions in particular must not be omitted: it is the sole data source for the Drawer's third line chart "angle (rad)"
    --   (frontend_dashboard/src/components/RobotDetailDrawer.tsx:88).
    last(joint_temperatures, time) AS joint_temperatures,
    last(joint_positions, time)    AS joint_positions,
    last(joint_torques, time)      AS joint_torques
FROM robot_telemetry
GROUP BY bucket, robot_id
WITH NO DATA;

-- Refresh policy: more aggressive than robot_status_10s (end_offset 10 seconds, schedule 30 seconds).
--
-- Why we cannot just copy 1 minute:
--   ISSUE #76's acceptance explicitly requires "when the Drawer opens, the data must be recent, a few seconds of delay is acceptable,
--   but it must not be stale to the minute level". robot_status_10s's end_offset=1min + schedule=1min
--   has a worst case of 2 minutes before visibility, which sits exactly on the prohibition line.
--   end_offset=10s (= one bucket width): only refresh "already-finalized buckets", without touching the current bucket still being written,
--      avoiding inconsistency where the same bucket is materialized first then backfilled by new data;
--   schedule=30s: 3x the bucket width; a single incremental refresh only scans a 30-second window,
--      about 30k rows at 1000 rows/s, far below the 300k-row full scan on the raw table.
--   start_offset kept at 1 hour: consistent with robot_status_10s, and leaves a window for "late-arriving data backfill".
SELECT add_continuous_aggregate_policy('robot_history_10s',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '10 seconds',
    schedule_interval => INTERVAL '30 seconds',
    if_not_exists => TRUE);

-- 5.1b Continuous aggregate (R-12): `/history` hourly downsampling, **structurally mirrors robot_history_10s**
--
-- Why it is needed:
--   `/history`'s `minutes` has no upper limit (main.py originally had `span_s = max(1, minutes) * 60`).
--   Querying `?minutes=1440` (24h), the 10s aggregate returns 8640 buckets/robot × 100 robots ≈ 860k points,
--   rendering 10s points in the Drawer would inevitably freeze; operators also cannot see the "whole-day trend".
--   Hourly buckets compress the same window into 24 points/robot -- both usable and readable; this is the only reason for this view's existence.
--
-- Why it is a **mirror** rather than reusing robot_history_10s + computing time_bucket('1 hour') on the fly:
--   The continuous aggregate's materialized table is stored at 10s granularity; computing 1h buckets on top of it still requires scanning 8640 rows/robot first;
--   This view materializes the "hourly granularity", cutting the scan volume by another order of magnitude (8640 → 24).
--   This is isomorphic to order 16's argument for using robot_history_10s instead of the raw table: downsampling must happen on the **write side**
--   ; computing on the read side merely turns a one-time cost into a per-request cost.
--
-- The column set **mirrors** robot_history_10s verbatim (including the three JSONB joint columns, especially joint_positions).
--   `joint_positions` must never be omitted: it is the sole data source for the Drawer's third line chart "angle (rad)"
--   (frontend_dashboard/src/components/RobotDetailDrawer.tsx:88);
--   Order 16 already truly failed once (missing column → permanently empty chart); R-12 §6 enshrines "must not omit columns" as a hard red line.
--   history()'s two branches reuse the same `_select` template, and the column names are the response field names:
--   once this view's columns drift from the 10s view's columns, the hourly path's response body will be missing fields.
--
-- ⚠️ The materialized_only footgun (R-13, verified on a real DB at 2.13.1): this view must use **real-time aggregation**
--   (`materialized_only` set to `false`), otherwise the `/history` long-span path has a 1h gap at its right end.
--   TimescaleDB **2.13.1 continuous aggregates default `materialized_only` to `true`** -- source
--   `src/ts_catalog/continuous_agg.c:58-62`'s `materialized_only.default_val = (Datum)true`;
--   only explicitly writing `materialized_only` as `false` when creating the view flips it. Real-DB measurement: this view is created with `materialized_only = t`,
--   and `end_offset=1h` + `WITH NO DATA` ⇒ the `[now-1h, now]` interval is unqueryable (the gap is real, not a false alarm).
--   ⇒ This requirement explicitly uses §5.1c `SET (timescaledb.materialized_only = false)` to enable real-time aggregation;
--     `end_offset=1h` (lines :254-258) only defines the materialization boundary; the right-side 1h real-time segment is transparently backfilled by the engine against `robot_telemetry`.
--   **Do not** set it back to `true` -- that would make the long-span `/history` right end show a 1h gap again (exactly what this R-13 fixes).
--   Note: this comment is only added to `robot_history_1h` / `robot_history_10s` (the two `/history` data sources);
--   `robot_status_10s` (dashboard latest state) is a sibling gap of the same kind, left unchanged this round per R-13 §3, to be evaluated in a separate requirement.
CREATE MATERIALIZED VIEW IF NOT EXISTS robot_history_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time)    AS bucket,
    robot_id,
    last(status_code, time)        AS status_code,
    last(battery_pct, time)        AS battery_pct,
    max(max_joint_temp)            AS max_joint_temp,
    last(error_code, time)         AS error_code,
    last(joint_temperatures, time) AS joint_temperatures,
    -- ↓ Must be kept: data source for the Drawer's third line chart, a hard red line per R-12 §6.
    last(joint_positions, time)    AS joint_positions,
    last(joint_torques, time)      AS joint_torques
FROM robot_telemetry
GROUP BY bucket, robot_id
WITH NO DATA;

-- Refresh policy: end_offset=1h (= one bucket width) / schedule=1h.
--
-- Why end_offset is set to one bucket width rather than copying the 10s view's 10 seconds:
--   The bucket width is the minimum unit for "when data is considered finalized". end_offset < bucket width makes the policy repeatedly refresh
--   **the current bucket still being written**, where the same bucket is materialized first then backfilled by new data, causing materialization-result jitter;
--   taking the full bucket width means only refreshing "sealed" buckets, with semantics consistent with the 10s view (10s view end_offset=10s).
-- schedule is of the same order as the bucket width (1h): a single incremental refresh only scans the most recent bucket-width window,
--   about 3.6M rows at 1000 rows/s -- this is the background cost of refresh, not on the online query path.
-- start_offset kept at 1 hour goes without saying; the hourly bucket's "late-arriving data backfill" window should reasonably be larger;
--   but since the raw table retains 30 days (§6.2), the late-arrival upper bound is constrained by it, so there is still ample margin.
--
-- Relationship with section 6: this view **does not create** separate retention/compression policies -- R-12 §4 T1 explicitly states "retention policy follows the existing one",
--   its lifecycle is governed by the underlying hypertable's retention policy (30 days); creating one again would interfere with section 6's policies.
SELECT add_continuous_aggregate_policy('robot_history_1h',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- 5.1c Continuous aggregate (R-13 fix): change the two `/history` data sources to **real-time aggregation**
--
-- Why it must exist (verified on a real DB, not speculation):
--   TimescaleDB 2.13.1 continuous aggregates default `materialized_only` to `true` (source
--   `src/ts_catalog/continuous_agg.c:58-62`'s `default_val = (Datum)true`).
--   Default true ⇒ the cagg's query view is a **pure materialized table** (`pg_get_viewdef` has no `UNION ALL ... FROM robot_telemetry`
--   real-time segment), and the `[now-end_offset, now]` interval is unqueryable -- i.e. the `/history` right-end gap (1h view 1h gap,
--   10s view 10s gap). The two `SET (materialized_only = false)` statements below enable real-time aggregation; the right-side real-time segment is handled by the engine
--   transparently backfilling against `robot_telemetry`, and the query SQL and column set are **byte-for-byte unchanged** (R-03 §4 contract unaffected).
--
-- Why only these two views are changed:
--   They are exactly the two `/history` data sources (`history()` routes by span to 10s / 1h, main.py:308-309).
--   `robot_status_10s` (dashboard latest state) is a sibling gap of the same kind, left unchanged this round per R-13 §3, to be evaluated in a separate requirement.
--
-- Idempotency (re-running `init.sql` is safe):
--   `ALTER MATERIALIZED VIEW ... SET (...)` re-executed on a view already at that value is a no-op and does not error;
--   `IF EXISTS` is still added as a safeguard so this block does not blow up when re-run on a database where the view was renamed/deleted.
--   ⚠️ Reverse red line: **do not** set `materialized_only` back to `true` -- that is exactly this bug itself
--   (the regression test tests/e2e/test_cagg_realtime_backfill.py will immediately turn red on a real DB).
ALTER MATERIALIZED VIEW IF EXISTS robot_history_10s SET (timescaledb.materialized_only = false);
ALTER MATERIALIZED VIEW IF EXISTS robot_history_1h  SET (timescaledb.materialized_only = false);

-- 5.1d Continuous aggregate (R-16 fix): change `robot_status_10s` (dashboard latest state) to **real-time aggregation**
--
-- Why it must exist (same root as R-13 in §5.1c, only the consumer differs):
--   TimescaleDB 2.13.1 continuous aggregates default `materialized_only = true` (source
--   `src/ts_catalog/continuous_agg.c:58-62`'s `default_val = (Datum)true`).
--   R-13 only flipped the two `/history` caggs (the two above, **this requirement must not modify/delete them**);
--   `robot_status_10s` was explicitly **left un-fixed** per R-13 §3 at the time. It serves "dashboard latest state",
--   `end_offset = 1 minute` + `materialized_only = true` ⇒ the dashboard's per-robot
--   current status/battery/temperature **lacks the most recent 1 minute**. This one fills that gap.
--
-- Idempotency (re-running `init.sql` is safe):
--   `ALTER MATERIALIZED VIEW ... SET (...)` re-executed on a view already at that value is a no-op and does not error;
--   `IF EXISTS` as a safeguard so this block does not blow up when re-run on a database where the view was renamed/deleted (same form as §5.1c).
--   ⚠️ Reverse red line: **do not** set `materialized_only = true` back -- that is exactly this bug itself
--   (the regression test tests/e2e/test_cagg_realtime_backfill.py will immediately turn red on a real DB).
--
-- This requirement **does not touch**: the refresh policy (the `add_continuous_aggregate_policy('robot_status_10s')` above
-- has start_offset/end_offset=1min/schedule_interval=1min all at their original values), the column set
-- (status_code/battery_pct/max_joint_temp/error_code), the frontend, and the `backend_ingestion` query.
-- `end_offset` only defines the materialization boundary; the right-side real-time segment is transparently backfilled by the engine against `robot_telemetry`.
ALTER MATERIALIZED VIEW IF EXISTS robot_status_10s SET (timescaledb.materialized_only = false);

-- 5.2 Materialized-table index notes (**no manual creation needed**)
-- Continuous aggregates automatically create a `(<grouping column>, <partitioning column> DESC)` index for each grouping column
--   (per the comment and
--    mattablecolumninfo_add_mattable_index implementation in tsl/src/continuous_aggs/create.c:303-306).
-- `/history`'s predicate is `robot_id = $2 AND bucket >= now() - span`,
-- which falls exactly on the automatic index (robot_id, bucket DESC), so no extra DDL is needed.

-- 6. Data lifecycle: compression + retention (I-17)
--
-- Why it must exist: chunk_time_interval = 7 days; without a retention policy, disk usage only grows.
-- Capacity estimate (per-column row width + 24B row header + 1.3x page overhead):
--   100 robots × 10Hz = 1000 row/s = 86,400,000 rows/day ≈ 1.34 KB/row ≈ **116 GB/day**.
--   30 days raw ≈ 3.5 TB; after compression (typical 10x for numeric columns) ≈ **350 GB**, fits on a single SSD.
--   90 days compressed ≈ 1.0 TB and up, requiring extra volume planning and backup windows, with unclear payback from review.
-- Decision: **raw telemetry retained 30 days, alerts retained 180 days** (rationale in docs/DECISIONS-issue19.md).
--
-- ⚠️ Idempotent implementation (replacing the old `if_not_exists => TRUE` form):
--    The `if_not_exists` parameter of `add_retention_policy` / `add_compression_policy`
--    **was not verified in this repo** (this machine has no TimescaleDB environment; external/timescaledb's
--    retention/sql.py and compression/sql.py do not expose that parameter).
--    Once the old form's parameter name does not hold, init.sql would **fail entirely**; hence we instead use
--    an existence check on `timescaledb_information.jobs` for idempotency, making re-runs safe.
--    View-name and column-name evidence: external/timescaledb/retention/sql.py:47-58.
-- ⚠️ Order must not be reversed: compression must be enabled via ALTER TABLE first, then add_compression_policy,
--    otherwise the policy job will fail because the hypertable has not enabled compression.
--    Syntax evidence: external/timescaledb/compression/sql.py:60-77.
--
-- Retention of 30 days > the continuous aggregate's start_offset(1h), so there is no "raw table already deleted while aggregate still pulling" gap.
-- This section is **not verified on this machine** (no TimescaleDB in this environment; PG15 lacks the extension).

-- 6.1 Telemetry compression: chunks older than 1 day become columnar (recent 1 day stays hot data; real-time queries avoid the decompression path)
ALTER TABLE robot_telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'robot_id',
    timescaledb.compress_orderby   = 'time DESC'
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression'
          AND hypertable_name = 'robot_telemetry'
    ) THEN
        PERFORM add_compression_policy('robot_telemetry', INTERVAL '1 day');
    END IF;
END $$;

-- 6.2 Telemetry retention: discard whole chunks after 30 days (retention is per-chunk, i.e. 7 days per chunk)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_retention'
          AND hypertable_name = 'robot_telemetry'
    ) THEN
        PERFORM add_retention_policy('robot_telemetry', INTERVAL '30 days');
    END IF;
END $$;

-- 6.3 Alert table: also a hypertable; low-frequency but equally needs guardrails.
--     Retained 180 days (needed for compliance/review, and volume is tiny: even 10k rows/day is only in the millions).
ALTER TABLE robot_alert SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'robot_id',
    timescaledb.compress_orderby   = 'time DESC'
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_compression'
          AND hypertable_name = 'robot_alert'
    ) THEN
        PERFORM add_compression_policy('robot_alert', INTERVAL '7 days');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM timescaledb_information.jobs
        WHERE proc_name = 'policy_retention'
          AND hypertable_name = 'robot_alert'
    ) THEN
        PERFORM add_retention_policy('robot_alert', INTERVAL '180 days');
    END IF;
END $$;

-- 7. Common indexes
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time ON robot_telemetry (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_robot_time ON robot_alert (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_ack ON robot_alert (ack) WHERE ack = FALSE;
-- Escalation scan (order 17 escalation ticker): only scans the narrow set of severity=3, unacked, and un-escalated.
-- The predicate matches escalation.py's WHERE verbatim; otherwise the index is unused (a partial index is only hit when the predicate is implied).
CREATE INDEX IF NOT EXISTS idx_alert_escalation ON robot_alert (time)
    WHERE ack = FALSE AND escalated = FALSE AND severity = 3;
-- Suppression view (order 35): the narrow-set index for `GET /alerts?suppressed=true|false`.
-- Same partial-index rationale as idx_alert_ack: suppressed rows are the minority (only produced when a higher severity exists on the same robot),
-- and the default view queries `suppressed = FALSE`, which is the hot path.
CREATE INDEX IF NOT EXISTS idx_alert_suppressed ON robot_alert (time DESC)
    WHERE suppressed = TRUE;


-- 8. Authenticated-user table (order 15: fastapi-users + two-tier RBAC)
--
-- Why a separate table instead of reusing robot_metadata: that is the ledger of "monitored objects",
-- while this is the "operator"'s account; the two have completely different lifecycles and permissions, and mixing them in one table
-- would eventually raise meaningless questions like "can a robot log in".
--
-- Columns correspond one-to-one with backend_ingestion/app/auth/users.py:RobotUser (including column order).
-- The first five columns are fastapi-users' contract columns; the last two are added by this project.
-- Character set/length follow the framework contract: email 320 is the RFC 5321 address upper limit,
-- hashed_password 1024 leaves headroom beyond bcrypt(60) for a future algorithm switch.
--
-- ⚠️ Privilege-escalation method: this change **does not provide** an admin endpoint; making a user an admin is done by ops directly editing the DB:
--     UPDATE robot_user SET role = 'admin' WHERE email = 'ops@example.com';
-- Self-registration can only yield viewer (see app/auth/manager.py:on_after_register).
CREATE TABLE IF NOT EXISTS robot_user (
    id              BIGSERIAL    PRIMARY KEY,
    email           VARCHAR(320) NOT NULL UNIQUE,
    hashed_password VARCHAR(1024) NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    is_superuser    BOOLEAN      NOT NULL DEFAULT FALSE,
    is_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Two-tier RBAC: viewer (read-only) / admin (read-only + alert ack).
    -- CHECK instead of ENUM: there are only two roles; adding a new value via CHECK is easier than an ENUM migration;
    -- the app-side source of truth remains the Role enum in app/auth/roles.py; this only serves as a fallback.
    role            VARCHAR(16)  NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('viewer', 'admin')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- email's UNIQUE constraint already implicitly creates an index; login queries by email, so no separate index is needed.
