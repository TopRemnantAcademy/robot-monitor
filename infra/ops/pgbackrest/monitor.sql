-- pgBackRest 备份状态的「数据库内」可观测入口（订单 31 §T3）
--
-- 为什么不另造一套告警通道：
--   #59 已把「告警落库失败」做成 Prometheus 指标（robot_alert_store_errors_total）
--   + 规则（infra/observability/rules/robot-alerts.yml 的 AlertStoreFailing），
--   由既有的 Alertmanager → webhook 通道推送。备份失败复用它，只补一个**指标来源**。
--
-- 指标来源的官方依据：
--   external/pgbackrest/doc/xml/user-guide.xml 的 Monitoring 段
--   → `pgbackrest --output=json info` 是**稳定**接口（reference.xml info 命令段：
--     "For machine-readable output use --output=json … kept stable unless a bug is found"）。
--   → 官方示例把 info 的 JSON 用 `COPY … FROM PROGRAM` 灌进 jsonb 再查询，
--     本文件逐字沿用该范式（doc/example/pgsql-pgbackrest-info.sql）。
--
-- 应用方式（在 timescaledb 容器内，见 docs/DR-RUNBOOK.md §5.2）：
--   psql -U postgres -d robot_monitor -f /opt/dr/monitor.sql
--
-- ⚠️ 依赖：DB 主机内必须有 `pgbackrest` 二进制（Dockerfile.timescaledb 装入）+ 能连仓库。
--    未启用 DR overlay 时调用 monitor.pgbackrest_info() 会失败 —— 这是**预期**，
--    该函数只在 DR 场景由 postgres-exporter 调用。

create schema if not exists monitor;

-- 官方示例（doc/example/pgsql-pgbackrest-info.sql）：把 info JSON 读进 jsonb。
create or replace function monitor.pgbackrest_info()
    returns jsonb AS $$
declare
    data jsonb;
begin
    create temp table temp_pgbackrest_data (data text) on commit drop;

    -- COPY FROM PROGRAM 是 PG 官方能力（server-side 执行），不需要 shell 包装。
    copy temp_pgbackrest_data (data)
        from program
            'pgbackrest --output=json info' (format text);

    select replace(temp_pgbackrest_data.data, E'\n', '\n')::jsonb
      into data
      from temp_pgbackrest_data;

    return data;
end $$ language plpgsql;

-- 便捷视图：把监控最关心的两个量摊平成一行，供 postgres-exporter 自定义查询消费。
-- 为什么用视图而不是让 exporter 直接调函数：
--   postgres-exporter 的 queries.yaml 只能写 SQL；视图让 SQL 保持简单、可单测。
create or replace view monitor.pgbackrest_backup_status as
with stanza as (
    select data->>'name' as stanza,
           data->'status'->>'code' as status_code,
           (data->'backup'->(jsonb_array_length(data->'backup') - 1)) as last_backup,
           (data->'archive'->(jsonb_array_length(data->'archive') - 1)) as current_archive
      from jsonb_array_elements(monitor.pgbackrest_info()) as data
)
select stanza,
       (status_code)::bigint as status_code,
       -- 最近一次成功备份的 stop 时间戳（epoch 秒）——「备份是否过期」的判据
       coalesce((last_backup->'timestamp'->>'stop')::numeric, 0)::bigint
           as last_backup_stop_epoch,
       -- 最近一次备份是否带 error 标记（备份集不完整）
       coalesce((last_backup->>'error')::boolean, true) as last_backup_errored,
       coalesce(current_archive->>'max', '') as last_archived_wal
  from stanza;
