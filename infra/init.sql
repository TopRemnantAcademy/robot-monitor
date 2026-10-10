-- robot_monitor init schema for TimescaleDB (PostgreSQL 16 + timescaledb 2.13)
-- 本文件在 timescaledb 容器首次启动时由 /docker-entrypoint-initdb.d/ 自动执行。

-- 1. 启用 timescaledb 扩展
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 2. 高频遥测表
-- 注意：复合主键必须包含分区列 time（TimescaleDB 硬性要求），故用 (time, robot_id)。
CREATE TABLE IF NOT EXISTS robot_telemetry (
    time              TIMESTAMPTZ     NOT NULL,
    robot_id          TEXT            NOT NULL,
    status_code       INTEGER         NOT NULL,   -- 0 离线 / 1 正常 / 2 预警 / 3 故障
    error_code        INTEGER         NOT NULL DEFAULT 0,
    battery_pct       DOUBLE PRECISION NOT NULL,
    battery_temp      DOUBLE PRECISION,
    max_joint_temp    DOUBLE PRECISION NOT NULL,  -- 预计算：所有关节最高温
    joint_temperatures JSONB          NOT NULL,
    joint_positions     JSONB         NOT NULL,
    joint_torques       JSONB         NOT NULL,
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

-- 3. 机器人元数据表（低频）
CREATE TABLE IF NOT EXISTS robot_metadata (
    robot_id         TEXT PRIMARY KEY,
    model_type       TEXT,
    firmware_version TEXT,
    assigned_station TEXT,
    is_online        BOOLEAN DEFAULT FALSE,
    last_seen        TIMESTAMPTZ
);

-- 3.1 预置 100 台机器人（humanoid-001 ~ humanoid-100）
-- 目的：看板在系统尚无数据时也能渲染 100 张卡片；后端 WS 快照可据此枚举 ID；
--       在线率的分母固定为 100，不依赖「已收到过数据的机器人数量」。
INSERT INTO robot_metadata (robot_id, model_type, firmware_version, assigned_station)
SELECT
    'humanoid-' || lpad(g::text, 3, '0'),
    'HUMANOID-24DOF',
    '1.0.0',
    'STATION-' || lpad(((g - 1) % 10 + 1)::text, 2, '0')
FROM generate_series(1, 100) AS g
ON CONFLICT (robot_id) DO NOTHING;

-- 4. 告警表
CREATE TABLE IF NOT EXISTS robot_alert (
    time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    robot_id  TEXT        NOT NULL,
    rule_id   TEXT        NOT NULL,   -- motor_temp | battery_low | offline | joint_stuck
    severity  INTEGER     NOT NULL,   -- 1 info / 2 warning / 3 critical
    message   TEXT        NOT NULL,
    detail    JSONB,
    ack       BOOLEAN DEFAULT FALSE
);

SELECT create_hypertable('robot_alert', 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '7 days');

-- 5. 连续聚合：每 10 秒一台机器人的最新快照（供看板/历史折线秒级刷新）
-- 已核实：TimescaleDB 2.7+ 的连续聚合支持 first()/last()（官方文档明确示例），
-- 本库锁定 2.13.1，故 last(...) 可用，无需改写为 max()。
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

SELECT add_continuous_aggregate_policy('robot_status_10s',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE);

-- 6. 常用索引
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time ON robot_telemetry (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_robot_time ON robot_alert (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_ack ON robot_alert (ack) WHERE ack = FALSE;

-- 7. 数据保留（TimescaleDB retention policy）
-- 为什么必须有：chunk_time_interval 是 7 天，若不设保留策略，磁盘只增不减。
-- 100 台 × 10Hz ≈ 8.64M 行/天，按本机实测（PG15，含索引约 12KB/千行量级）估算，
-- 30 天原始表就是数百 GB 量级 —— 工厂看板不需要无限期保留原始高频点。
--
-- 保留 30 天原始数据 + 连续聚合按 chunk 自动级联删除。
-- 注意：add_retention_policy 会级联删除同一时间范围内的连续聚合物化数据，
-- 因此 30 天 > 聚合的 start_offset(1h)，不会出现「原始表已删、聚合还在拉」的空洞。
--
-- 本语句**未在本机验证**（本环境无 Docker/TimescaleDB，PG15 无 timescaledb 扩展）。
-- 依据：TimescaleDB 2.x 官方 API `add_retention_policy(hypertable, drop_after)`。
SELECT add_retention_policy('robot_telemetry',
    drop_after => INTERVAL '30 days',
    if_not_exists => TRUE);

-- 告警保留更久（合规/复盘需要），90 天
SELECT add_retention_policy('robot_alert',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);

-- 8. 压缩策略（可选）：把 7 天前的 chunk 按 robot_id 分段列式压缩，
--    压缩率通常 10x 以上，同时保留查询能力（透明解压）。
--    未验证：需 TimescaleDB 2.13 环境实测压缩率后再决定是否启用，故此处先注释掉，
--    避免「写了却没验证过」的策略悄悄生效。启用前请跑 EXPLAIN 确认查询计划未劣化。
--
-- ALTER TABLE robot_telemetry SET (
--     timescaledb.compress,
--     timescaledb.compress_segmentby = 'robot_id',
--     timescaledb.compress_orderby = 'time DESC'
-- );
-- SELECT add_compression_policy('robot_telemetry', INTERVAL '7 days', if_not_exists => TRUE);

