-- Migration 002: add policy_actions hypertable (R-VLA-EVAL Phase 1)
--
-- PURPOSE
--   Add the VLA policy-action stream WITHOUT touching the existing
--   `robot_telemetry` table or its 38w+ rows. This is applied manually to a
--   database that was initialized by the original init.sql; init.sql itself is
--   only run on first start by /docker-entrypoint-initdb.d/ and will NOT
--   recreate tables for an already-existing database.
--
-- APPLY (from the timescaledb container or any psql with superuser on robot_monitor):
--   psql "$DATABASE_URL" -f infra/migrations/002_policy_actions.sql
--
-- IDEMPOTENT: every statement is CREATE IF NOT EXISTS / if_not_exists => TRUE /
--   existence-guarded, so re-running is safe.

-- 1. Table (PK must include the partitioning column `time`, hence (time, robot_id)).
CREATE TABLE IF NOT EXISTS policy_actions (
    time              TIMESTAMPTZ     NOT NULL,
    robot_id          TEXT            NOT NULL,
    policy_version    TEXT            NOT NULL,
    trial_id          TEXT,
    action            JSONB           NOT NULL,
    goal              TEXT,
    PRIMARY KEY (time, robot_id)
);

-- 2. Hypertable (idempotent).
SELECT create_hypertable('policy_actions', 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

-- 3. Compression + retention (guarded against re-applying).
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

-- 4. Indexes.
CREATE INDEX IF NOT EXISTS idx_policy_actions_robot_time ON policy_actions (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_policy_actions_trial ON policy_actions (trial_id) WHERE trial_id IS NOT NULL;
