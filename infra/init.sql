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
--
-- 身份语义（订单 19）：`alert_id BIGSERIAL` + `PRIMARY KEY (time, alert_id)`。
--   - 主键必须含分区列 time（TimescaleDB 硬性要求），故顺序为 (time, alert_id)。
--   - alert_id 单调自增，唯一标识「同机器人同时刻的哪一条」告警：
--     alert_engine 的一轮 tick 内，motor_temp / battery_low / joint_stuck 各有独立
--     去重窗口（alert_engine/app/main.py 的 DEDUP_WINDOW），同一 (time, robot_id)
--     可以合法对应多条不同规则告警 —— 旧语义「(time, robot_id) 即身份」是错的。
--   - ack_at / ack_by 用于把「已确认」落到具体某一条并记录确认时间/人；
--     本轮仅建字段，ack 接口属订单 17（告警闭环）范围。
--
-- ⚠️ 存量库迁移不在本单范围：本文件是**初始化**脚本（首次启动由
--    /docker-entrypoint-initdb.d/ 执行），对已存在的库不会重建表。
--    已有数据的库需手工执行：
--      ALTER TABLE robot_alert ADD COLUMN alert_id BIGSERIAL;
--      ALTER TABLE robot_alert ADD COLUMN ack_at TIMESTAMPTZ;
--      ALTER TABLE robot_alert ADD COLUMN ack_by TEXT;
--      ALTER TABLE robot_alert ADD PRIMARY KEY (time, alert_id);
--    （TimescaleDB 侧上述语句未在本机验证，见文件尾部说明。）
--
--    订单 17 追加 escalated（升级标记）列，存量库补迁移：
--      ALTER TABLE robot_alert ADD COLUMN escalated BOOLEAN NOT NULL DEFAULT FALSE;
--    本机已在纯 PG15.19 上实测该语句（见 PR「实测」章节）；ack_by/ack_at
--    由订单 19 加过，本单**不重复 ADD COLUMN**。
--
--    订单 35 追加 suppressed / suppressed_by（抑制标记，全栈贯通）：
--      ALTER TABLE robot_alert ADD COLUMN suppressed BOOLEAN NOT NULL DEFAULT FALSE;
--      ALTER TABLE robot_alert ADD COLUMN suppressed_by TEXT;
--    背景：订单 30 落地了抑制「判定与记录」（InhibitionRecord + WARNING 日志 +
--    robot_alert_suppressed_total 指标），但被抑制的告警**没有写库**，
--    于是 `GET /alerts` 永远查不到它 —— 值班员只能 grep 日志。
--    本单把「被压制的那条」也落库并置 suppressed=TRUE，使抑制在 REST 契约里可查、
--    前端可标「已抑制」。**不改变抑制判定语义**（订单 30 的「不静默丢弃」硬约束反而是靠
--    这一步才真正闭环：`InhibitionRecord` 在生产路径上原本无人落库）。
--    存量库补迁移时上面两条 ALTER 即可（DEFAULT FALSE 保证历史行语义不变）。
CREATE TABLE IF NOT EXISTS robot_alert (
    time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- alert_id 紧跟分区列 time：列序与 ORM 声明序、PK 序三者一致，便于逐列比对。
    alert_id  BIGSERIAL NOT NULL,
    robot_id  TEXT        NOT NULL,
    rule_id   TEXT        NOT NULL,   -- motor_temp | battery_low | offline | joint_stuck
    severity  INTEGER     NOT NULL,   -- 1 info / 2 warning / 3 critical
    message   TEXT        NOT NULL,
    detail    JSONB,
    ack       BOOLEAN     NOT NULL DEFAULT FALSE,
    ack_at    TIMESTAMPTZ,
    ack_by    TEXT,
    -- escalated：severity=3 且超时未 ack 时置 TRUE（订单 17 升级闭环）。
    -- 由 alert_engine/app/escalation.py 的 ticker 写入；前端据此显示「⚠ 已升级」。
    escalated BOOLEAN     NOT NULL DEFAULT FALSE,
    -- suppressed：本条告警**被抑制**（订单 35）。订单 30 的 suppress_record 把它连同
    -- 可追溯凭证一起写入，此前只写日志与指标、不落库，故 REST 契约查不到。
    -- 语义：TRUE = 已判定被压制（不推 WS、不通知）；但**行保留**，可查可审计。
    suppressed    BOOLEAN NOT NULL DEFAULT FALSE,
    -- suppressed_by：压制它的**根因规则**（如 offline / motor_temp）。
    -- 与 suppressed 成对：置 TRUE 时必非空。TEXT 而非外键 —— 规则是代码里的资产
    -- （app/rules/*.py 的 rule_id），没有对应的规则表可引用。
    -- 为什么不把源 severity / reason 也落列：那是 `detail` JSONB 的职责（无 schema 漂移风险）。
    suppressed_by TEXT,
    -- 主键必须含分区列 time（TimescaleDB 硬性要求）
    PRIMARY KEY (time, alert_id)
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

-- 5.1 连续聚合（ISSUE #76）：`/history` 专用，**含 JSONB 关节列**
--
-- 为什么必须新增而不是扩展 robot_status_10s：
--   ① `/history` 需要 joint_positions / joint_temperatures / joint_torques 三列
--      （Drawer 三条折线与热力图的数据源），而 robot_status_10s 只有标量列；
--   ② robot_status_10s 已被 R-08 §A4（视图存在）、test_integration.sh:172、
--      tests/e2e/test_e2e_30k.py 三处断言，改列会动摇这些既有验收面；
--   ③ 两个聚合的**消费方与刷新语义不同**：robot_status_10s 服务「看板最新态」，
--      本视图服务「单机历史回放」。合表会让两者的保留/刷新策略互相牵制。
--
-- 为什么 JSONB 能进连续聚合（源码实证，非推测；本机拉取 timescaledb 2.13.1
-- 源码核对，路径见 external/SOURCES.md 的 N21 条目）：
--   ① `last(value, time)` 的第一参数是 **anyelement 多态**
--      （timescaledb `src/agg_bookend.c:433` 的 `ts_last_sfunc` 签名
--      `last(internal, anyelement, "any")`），JSONB 与 INTEGER 走同一条路径，
--      不存在「JSONB 不支持聚合」这一限制；旧注释所述「缺列」是当初**没写**，不是不能写。
--   ② 内部 partialize 走 `_timescaledb_functions.partialize_agg(arg ANYELEMENT)`
--      （`sql/partialize_finalize.sql:6-7`），同样多态，JSONB 可直接落物化表。
--   ③ 列序无隐藏要求：连续聚合的**分区列**由 `time_bucket` 所在的 TargetEntry
--      在 targetlist 中的位置决定（`tsl/src/continuous_aggs/finalize.c:875-877`
--      设置 `matpartcolno/matpartcolname`），并不要求它排第一位；
--      `caggtimebucket_validate` 只校验「恰好一个 time_bucket，且作用于分区列」
--      （`tsl/src/continuous_aggs/common.c:152-206`），不校验位置。
--      下面仍把 bucket 放首列 —— 读起来最直观，且与 robot_status_10s 保持同构。
--
-- 键名与列名逐字对齐 robot_status_10s 的既有列，外加三个关节列；
-- 列名即 `/history` 返回体的字段名，两侧漂移会直接让 Drawer 变空。
CREATE MATERIALIZED VIEW IF NOT EXISTS robot_history_10s
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('10 seconds', time) AS bucket,
    robot_id,
    last(status_code, time)        AS status_code,
    last(battery_pct, time)        AS battery_pct,
    max(max_joint_temp)            AS max_joint_temp,
    last(error_code, time)         AS error_code,
    -- ↓ 三个 JSONB 关节列：ISSUE #76 的核心，也是订单 16「漏列→曲线恒空」的反面教训。
    --   joint_positions 尤其不能省：它是 Drawer 第三条折线「角度(rad)」的唯一数据源
    --   （frontend_dashboard/src/components/RobotDetailDrawer.tsx:88）。
    last(joint_temperatures, time) AS joint_temperatures,
    last(joint_positions, time)    AS joint_positions,
    last(joint_torques, time)      AS joint_torques
FROM robot_telemetry
GROUP BY bucket, robot_id
WITH NO DATA;

-- 刷新策略：比 robot_status_10s 更激进（end_offset 10 秒，schedule 30 秒）。
--
-- 为什么不能照抄 1 分钟：
--   ISSUE #76 验收明确要求「Drawer 打开时数据必须是最近的，可接受数秒延迟，
--   但不能陈旧到分钟级」。robot_status_10s 的 end_offset=1min + schedule=1min
--   最坏情况 2 分钟才可见，正好踩在禁止线上。
--   end_offset=10s（= 一个桶宽）：只刷新「已确定的桶」，不触碰仍在写入的当前桶，
--     避免同一桶先物化、后被新数据补齐导致的不一致；
--   schedule=30s：3 倍于桶宽，单次增量刷新只扫 30 秒窗口，
--     在 1000 行/s 下约 3 万行，远低于原始表 30 万行的全量扫描。
--   start_offset 保留 1 小时：与 robot_status_10s 一致，也给「迟到数据补写」留窗口。
SELECT add_continuous_aggregate_policy('robot_history_10s',
    start_offset => INTERVAL '1 hour',
    end_offset   => INTERVAL '10 seconds',
    schedule_interval => INTERVAL '30 seconds',
    if_not_exists => TRUE);

-- 5.2 物化表索引说明（**不需要手工建**）
-- 连续聚合会自动为每个分组列建 `(<分组列>, <分区列> DESC)` 索引
--   （tsl/src/continuous_aggs/create.c:303-306 的注释与
--    mattablecolumninfo_add_mattable_index 实现）。
-- `/history` 的谓词是 `robot_id = $2 AND bucket >= now() - span`，
-- 正落在自动索引 (robot_id, bucket DESC) 上，无需额外 DDL。

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
-- 升级扫描（订单 17 escalation ticker）：只扫 severity=3 且未确认、未升级的窄集。
-- 谓词与 escalation.py 的 WHERE 逐字对齐，否则索引不被使用（部分索引只在谓词蕴含时命中）。
CREATE INDEX IF NOT EXISTS idx_alert_escalation ON robot_alert (time)
    WHERE ack = FALSE AND escalated = FALSE AND severity = 3;
-- 抑制视图（订单 35）：`GET /alerts?suppressed=true|false` 的窄集索引。
-- 与 idx_alert_ack 同理用部分索引：被抑制行是少数（只有同机存在更高 severity 时才产生），
-- 且默认视图查 `suppressed = FALSE`，它才是热路径。
CREATE INDEX IF NOT EXISTS idx_alert_suppressed ON robot_alert (time DESC)
    WHERE suppressed = TRUE;


-- 8. 认证用户表（订单 15：fastapi-users + 两级 RBAC）
--
-- 为什么单独建表而不是复用 robot_metadata：那是"被监控对象"的台账，
-- 这是"操作人"的账号，两者生命周期与权限完全不同，混在一张表里
-- 迟早会出现"某台机器人能不能登录"这种没有意义的问题。
--
-- 列与 backend_ingestion/app/auth/users.py:RobotUser 逐列对应（含列序）。
-- 前五列是 fastapi-users 的契约列，最后两列是本项目追加的。
-- 字符集/长度按框架契约取：email 320 是 RFC 5321 的地址上限，
-- hashed_password 1024 是 bcrypt(60) 之外给未来换算法留的余量。
--
-- ⚠️ 提权方式：本单**不提供**管理端点，把用户改成管理员由运维直接改库：
--     UPDATE robot_user SET role = 'admin' WHERE email = 'ops@example.com';
-- 自助注册只能得到 viewer（见 app/auth/manager.py:on_after_register）。
CREATE TABLE IF NOT EXISTS robot_user (
    id              BIGSERIAL    PRIMARY KEY,
    email           VARCHAR(320) NOT NULL UNIQUE,
    hashed_password VARCHAR(1024) NOT NULL,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    is_superuser    BOOLEAN      NOT NULL DEFAULT FALSE,
    is_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
    -- 两级 RBAC：viewer（只读）/ admin（只读 + 告警确认）。
    -- CHECK 而非 ENUM：角色集合就两个，加新值时改 CHECK 比 ENUM 迁移省事；
    -- 应用侧的真值源仍是 app/auth/roles.py 的 Role 枚举，这里只做兜底。
    role            VARCHAR(16)  NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('viewer', 'admin')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- email 的唯一约束已由 UNIQUE 隐式建索引；登录按 email 查，故不必另建索引。
