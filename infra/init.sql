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

-- 6. 数据生命周期：压缩 + 保留（I-17）
--
-- 为什么必须有：chunk_time_interval = 7 天，不设保留策略磁盘只增不减。
-- 容量估算（逐列行宽 + 行头 24B + 页开销 1.3x）：
--   100 台 × 10Hz = 1000 row/s = 86,400,000 行/天 ≈ 1.34 KB/行 ≈ **116 GB/天**。
--   30 天原始 ≈ 3.5 TB；压缩（数值列典型 10x）后 ≈ **350 GB**，单机 SSD 可容纳。
--   90 天压缩后 ≈ 1.0 TB 起，需额外卷规划与备份窗口，复盘收益不明显。
-- 决策：**原始遥测保留 30 天，告警保留 180 天**（依据见 docs/DECISIONS-issue19.md）。
--
-- ⚠️ 幂等实现（替代旧的 `if_not_exists => TRUE` 写法）：
--    `add_retention_policy` / `add_compression_policy` 的 `if_not_exists` 参数
--    **未在本仓验证过**（本机无 TimescaleDB 环境；external/timescaledb 的
--    retention/sql.py 与 compression/sql.py 均未暴露该参数）。
--    旧写法一旦参数名不成立，init.sql 会**整段失败**；故改用
--    `timescaledb_information.jobs` 存在性判断做幂等，重跑安全。
--    视图名与列名实证：external/timescaledb/retention/sql.py:47-58。
-- ⚠️ 顺序不能反：必须先 ALTER TABLE 开启压缩，再 add_compression_policy，
--    否则 policy job 会因 hypertable 未启用压缩而失败。
--    语法实证：external/timescaledb/compression/sql.py:60-77。
--
-- 保留 30 天 > 连续聚合的 start_offset(1h)，不会出现「原始表已删、聚合还在拉」的空洞。
-- 本段**未在本机验证**（本环境无 TimescaleDB，PG15 无该扩展）。

-- 6.1 遥测压缩：1 天前的 chunk 转列式（近 1 天保持热数据，实时查询不走解压路径）
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

-- 6.2 遥测保留：30 天后整块丢弃（retention 以 chunk 为粒度，即 7 天一个块）
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

-- 6.3 告警表：同属超表，低频但同样需要护栏。
--     保留 180 天（合规/复盘需要，且体量极小：即便每天 1 万条也只有百万级）。
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

-- 7. 常用索引
CREATE INDEX IF NOT EXISTS idx_telemetry_robot_time ON robot_telemetry (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_robot_time ON robot_alert (robot_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_alert_ack ON robot_alert (ack) WHERE ack = FALSE;

