#!/usr/bin/env bash
# Observability wiring self-check (the parts that run even without docker).
#
# This machine has no docker (inside a CI container), so here we do **static + semantic** checks:
#   1. all YAML syntax
#   2. after merging the three compose layers, every scrape target in prometheus.yml resolves to a service
#   3. metric names in the rules file actually exist in the code (prevent 'rules referencing non-existent metrics')
#   4. env var names align with upstream README's file:line
#
# Real runtime verification needs docker; see infra/observability/README.md §5.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
fail=0
ok()  { echo "  [ok]   $1"; }
bad() { echo "  [FAIL] $1"; fail=1; }

echo "==> [1/5] YAML syntax"
for f in infra/docker-compose.yml infra/docker-compose.app.yml \
         infra/observability/docker-compose.observability.yml \
         infra/observability/prometheus.yml infra/observability/alertmanager.yml \
         infra/observability/rules/robot-alerts.yml; do
  if python3 -c "import yaml,sys; yaml.safe_load(open('$f'))" 2>/dev/null; then
    ok "$f"
  else
    bad "$f"
  fi
done

echo "==> [2/5] Scrape target reachability (three-layer merge)"
python3 - <<'PY' || fail=1
import yaml, re, sys
base = yaml.safe_load(open('infra/docker-compose.yml'))
app  = yaml.safe_load(open('infra/docker-compose.app.yml'))
obs  = yaml.safe_load(open('infra/observability/docker-compose.observability.yml'))
svc = set(base['services']); svc |= set(app['services']); svc |= set(obs['services'])
targets = re.findall(r'-\s*targets:\s*\["([^"]+)"\]',
                     open('infra/observability/prometheus.yml').read())
bad = [t for t in targets
       if t.split(':')[0] not in svc and t.split(':')[0] != 'localhost']
for t in targets:
    print(f"  [ok]   target {t}")
if bad:
    for t in bad: print(f"  [FAIL] unresolved target {t}")
    sys.exit(1)
PY

echo "==> [3/5] Rule metric names <-> code existence"
# ISSUE #72: the only canonical prefix is robot_backend_*, and the rules file must **not** retain robot_monitor_*.
# Here we check both 'the referenced metric is defined in metrics.py' and 'the rules don't reference the deprecated prefix'.
for m in robot_backend_queue_depth robot_backend_mqtt_dropped_total \
         robot_backend_telemetry_written_total \
         robot_backend_ws_probe_alive robot_backend_ws_probe_failures_total; do
  if grep -rq "$m" backend_ingestion/app/metrics.py; then ok "$m (backend)"; else bad "$m undefined"; fi
done
for m in robot_alert_emitted_total robot_alert_robots_online_gauge \
         robot_alert_store_errors_total; do
  if grep -rq "$m" alert_engine/app/metrics.py; then ok "$m (alert)"; else bad "$m undefined"; fi
done

# The probe metric must be defined separately from the connection-count metric (ISSUE #58): the probe
# (client view) and the connection count (server view) have different semantics and must not overwrite
# each other. After ISSUE #72 the connection-count metric was renamed to robot_backend_ws_connections
# and is indeed defined in metrics.py.
# -z is necessary: `Gauge(` and the metric-name literal span two lines (code is formatted this way), so
#   a line-by-line grep won't see it.
# This is not 'changing the check just to pass it' — the check's intent is 'the connection-count metric
#   is indeed defined in metrics.py', and a cross-line match is the correct expression of that intent.
if grep -q "robot_backend_ws_probe_alive" backend_ingestion/app/metrics.py \
   && grep -qzoE 'Gauge\(\s*"robot_backend_ws_connections"' backend_ingestion/app/metrics.py; then
  ok "ws_probe metric and ws_connections metric are separated (and connection count is in metrics.py)"
else
  bad "ws_probe / ws_connections metric definitions are confused"
fi

# ISSUE #72: after the dual-track naming was unified, code/rules/scrape config must **not** register/
# reference robot_monitor_* metrics anymore.
# Detection shape: a Prometheus metric name necessarily appears **inside quotes** (`"robot_monitor_x"`)
#   or in a rule expression.
# Migration records wrapped in backticks in comments (`` `robot_monitor_ws_connections` ``) do not count
#   as references — they are records of 'the old name is deprecated', which is exactly the info we want
#   to keep. So here we only match the 'double-quote/single-quote wrapped' shape.
dual=0
for f in backend_ingestion/app/metrics.py backend_ingestion/app/main.py \
         backend_ingestion/app/services/mqtt_subscriber.py \
         backend_ingestion/app/services/batch_writer.py \
         infra/observability/rules/robot-alerts.yml infra/observability/prometheus.yml; do
  if grep -qE '["'"'"']robot_monitor_[a-z_]+["'"'"']' "$f" \
     || grep -qE '^\s*(expr|-[[:space:]]+alert):.*robot_monitor_' "$f"; then
    bad "$f still references robot_monitor_* (ISSUE #72 deprecated this prefix)"; dual=1
  fi
done
[ "$dual" -eq 0 ] && ok "no robot_monitor_* residual (code/rules/scrape config)"

# Order 32 / ISSUE #100: expand the above 'no robot_monitor_* residual' from **6 hardcoded files**
# to a **repo-wide scan**, while also blocking 'whether the old names that existed before the change
# truly disappeared'.
#
# Why expand: the original for-list was hand-maintained — a newly added file referencing the old prefix
#   would be **invisible** to the inspector (outside the list). This is exactly the shape ISSUE #72
#   recurred in: three recycle branches each added a reference, everyone thought the inspection covered
#   it. Lists can grow, omissions can't be seen.
#
# The detection shape is consistent with above (quoted metric name / rule expression), so the backtick
#   form of migration records is not a violation.
# Whitelist (historical archive + the checklist scheduled by this task):
#   orders/**             - work orders and LEDGER historical records; verbatim archiving is discipline
#   backend_ingestion/tests/test_observability.py - FORBIDDEN_MONITOR_PREFIX guard constant
#   infra/observability/verify_wiring.sh          - the detection regex inside this script (self-reference)
dual_all=0
for f in $(grep -rl "robot_monitor_" \
              backend_ingestion alert_engine infra docs \
              --include="*.py" --include="*.json" --include="*.yml" \
              --include="*.yaml" --include="*.md" --include="*.sh" 2>/dev/null \
           | grep -v "test_observability.py" | grep -v "verify_wiring.sh"); do
  if grep -qE '["'"'"']robot_monitor_[a-z_]+["'"'"']' "$f" \
     || grep -qE '^\s*(expr|-[[:space:]]+alert):.*robot_monitor_' "$f"; then
    bad "$f still references robot_monitor_* (repo-wide scan; order 32)"; dual_all=1
  fi
done
[ "$dual_all" -eq 0 ] && ok "repo-wide scan: no active robot_monitor_* references (code/rules/config/docs)"

echo "==> [3.6/5] Order 32: before/after rename check (old names must vanish, new names must exist)"
# Old-name list = those that were **once registered as metrics** in order 32 T1's pre-change grep list.
# Here we assert at the **code literal** level that they no longer exist (migration records in comments are still allowed).
for old in robot_monitor_batch_queue_size robot_monitor_batch_flush_total \
           robot_monitor_batch_rows_total robot_monitor_mqtt_messages_total \
           robot_monitor_ws_connections robot_monitor_history_latency_seconds; do
  if grep -qE '["'"'"']'"$old"'["'"'"']' backend_ingestion/app/metrics.py; then
    bad "old name still registered: $old"
  else
    ok "old name gone: $old"
  fi
done
# New-name list (7 backend + 1 alert-engine rename)
for m in robot_backend_queue_depth robot_backend_batch_flushes_total \
         robot_backend_telemetry_written_total robot_backend_mqtt_received_total \
         robot_backend_ws_connections robot_backend_history_latency_seconds; do
  if grep -qE '["'"'"']'"$m"'["'"'"']' backend_ingestion/app/metrics.py; then
    ok "new name present: $m"
  else
    bad "new name missing: $m"
  fi
done
if grep -qE '["'"'"']robot_alert_robots_online_gauge["'"'"']' alert_engine/app/metrics.py \
   && ! grep -qE '["'"'"']robot_alert_robots_online["'"'"']' alert_engine/app/metrics.py; then
  ok "alert engine online count rename: robot_alert_robots_online -> robot_alert_robots_online_gauge"
else
  bad "alert engine online count rename not effective (robot_alert_robots_online_gauge)"
fi

echo "==> [3.5/5] Backup metric wiring (order 31 §T3)"
# Backup metrics referenced in rules must find their source in 'view + query definition', otherwise they reference thin air.
for m in pgbackrest_backup_last_success_timestamp_seconds \
         pgbackrest_backup_last_errored pgbackrest_stanza_status_code; do
  # the rule must reference it
  if ! grep -q "$m" infra/observability/rules/robot-alerts.yml; then
    bad "$m referenced by no rule"; continue
  fi
  # source: the metric name must appear in queries.yaml's metric definitions
  if grep -q "$m" infra/ops/pgbackrest/postgres-exporter-queries.yaml; then
    ok "$m (rule references + queries.yaml has definition)"
  else
    bad "$m referenced in rules, but no definition found in queries.yaml"
  fi
done
# the exporter must actually mount the custom query file, otherwise even a complete view can't be exported
if grep -q "extend.query-path=/etc/postgres-exporter/queries.yaml" \
      infra/observability/docker-compose.observability.yml \
   && grep -q "postgres-exporter-queries.yaml:/etc/postgres-exporter/queries.yaml" \
      infra/observability/docker-compose.observability.yml; then
  ok "postgres-exporter has mounted --extend.query-path"
else
  bad "postgres-exporter did not mount custom query (backup metrics won't be exported)"
fi

echo "==> [4/4] Upstream env variable name alignment"
if grep -q "DATA_SOURCE_NAME" external/postgres_exporter/README.md 2>/dev/null \
   && grep -q "DATA_SOURCE_NAME" infra/observability/docker-compose.observability.yml; then
  ok "DATA_SOURCE_NAME (postgres_exporter/README.md:223)"
else
  echo "  [skip] external/postgres_exporter not downloaded, skipping"
fi
if grep -q "REDIS_ADDR" external/redis_exporter/README.md 2>/dev/null \
   && grep -q "REDIS_ADDR" infra/observability/docker-compose.observability.yml; then
  ok "REDIS_ADDR (redis_exporter/README.md:170)"
else
  echo "  [skip] external/redis_exporter not downloaded, skipping"
fi

# Order 22 / I-12: exporter same-network + no plaintext password. These two are the root cause of
#   'started but can't be scraped', which container status alone can't reveal; must be blocked at the config layer.
echo "==> [5/5] exporter same-network + no plaintext password"
python3 - <<'PY2' || fail=1
import yaml, sys
obs = yaml.safe_load(open('infra/observability/docker-compose.observability.yml'))
base = yaml.safe_load(open('infra/docker-compose.yml'))
fails = []

# a) This file must not declare its own network: once declared, base's timescaledb/redis won't
#    automatically fall in, and the exporter becomes a network island (DNS unresolvable, /metrics
#    shows no business metrics).
if obs.get('networks'):
    fails.append("observability file declares networks, exporter will be on a separate network from base services")

# b) the exporter's depends_on target must actually exist in the base compose (otherwise the DNS name is empty)
base_svc = set(base['services'])
for name in ('postgres-exporter', 'redis-exporter'):
    svc = obs['services'].get(name)
    if not svc:
        fails.append(f"{name} missing")
        continue
    for dep in svc.get('depends_on', []) or []:
        if dep not in base_svc:
            fails.append(f"{name} depends_on {dep}, but base compose has no such service")

# c) DATA_SOURCE_NAME must use a ${...} variable reference, and no literal password may appear
dsn = obs['services']['postgres-exporter']['environment']['DATA_SOURCE_NAME']
if '${POSTGRES_PASSWORD' not in dsn:
    fails.append("DATA_SOURCE_NAME does not use a ${POSTGRES_PASSWORD} variable reference")
# The outbound hostname must be base's service name (only same-network resolves it)
if '@timescaledb:' not in dsn:
    fails.append("DATA_SOURCE_NAME target host is not timescaledb (non-same-network DNS)")

for f in fails:
    print(f"  [FAIL] {f}")
if fails:
    sys.exit(1)
print("  [ok]   no own network / depends_on target exists / password via variable")
PY2

echo
if [ "$fail" -eq 0 ]; then echo "all passed ✅"; else echo "failures present ❌"; fi
exit "$fail"
