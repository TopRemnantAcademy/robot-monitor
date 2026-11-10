-- In-DB observability entry point for pgBackRest backup status (order 31 §T3)
--
-- Why not build a separate alerting channel:
--   #59 already turned 'alert store failure' into a Prometheus metric (robot_alert_store_errors_total)
--   + a rule (AlertStoreFailing in infra/observability/rules/robot-alerts.yml),
--   pushed through the existing Alertmanager → webhook channel. Backup failure reuses it, adding only a **metric source**.
--
-- Official basis for the metric source:
--   the Monitoring section of external/pgbackrest/doc/xml/user-guide.xml
--   → `pgbackrest --output=json info` is a **stable** interface (reference.xml info command section:
--     "For machine-readable output use --output=json … kept stable unless a bug is found").
--   → the official example pipes info's JSON into jsonb via `COPY … FROM PROGRAM` then queries it,
--     and this file follows that pattern verbatim (doc/example/pgsql-pgbackrest-info.sql).
--
-- How to apply (inside the timescaledb container, see docs/DR-RUNBOOK.md §5.2):
--   psql -U postgres -d robot_monitor -f /opt/dr/monitor.sql
--
-- ⚠️ Dependency: the DB host must have the `pgbackrest` binary (installed in Dockerfile.timescaledb) + be able to reach the repo.
--   Calling monitor.pgbackrest_info() without the DR overlay enabled will fail — this is **expected**,
--   the function is only called by postgres-exporter in DR scenarios.

create schema if not exists monitor;

-- Official example (doc/example/pgsql-pgbackrest-info.sql): read the info JSON into jsonb.
create or replace function monitor.pgbackrest_info()
    returns jsonb AS $$
declare
    data jsonb;
begin
    create temp table temp_pgbackrest_data (data text) on commit drop;

    -- COPY FROM PROGRAM is a PG native capability (server-side execution), no shell wrapper needed.
    copy temp_pgbackrest_data (data)
        from program
            'pgbackrest --output=json info' (format text);

    select replace(temp_pgbackrest_data.data, E'\n', '\n')::jsonb
      into data
      from temp_pgbackrest_data;

    return data;
end $$ language plpgsql;

-- Convenience view: flatten the two most-monitored quantities into one row for postgres-exporter custom queries.
-- Why a view rather than letting the exporter call the function directly:
--   postgres-exporter's queries.yaml can only contain SQL; the view keeps the SQL simple and testable.
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
       -- stop timestamp (epoch seconds) of the last successful backup — the criterion for "whether backup is stale"
       coalesce((last_backup->'timestamp'->>'stop')::numeric, 0)::bigint
           as last_backup_stop_epoch,
       -- whether the last backup carries an error flag (backup set incomplete)
       coalesce((last_backup->>'error')::boolean, true) as last_backup_errored,
       coalesce(current_archive->>'max', '') as last_archived_wal
  from stanza;
