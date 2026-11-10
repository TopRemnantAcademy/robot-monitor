#!/usr/bin/env bash
# Fetch the 14 upstream open-source reference sources under external/
# Source and selection rationale: see external/SOURCES.md; integration points: external/INTEGRATION-PLAN.md.
# Usage: bash scripts/fetch-external-refs.sh [target dir, default external]
set -uo pipefail

DEST="${1:-external}"
mkdir -p "$DEST"
cd "$DEST"

# All are official GitHub org repos, cloned shallow with --depth 1
REPOS=(
  "https://github.com/emqx/mqtt-to-timescaledb.git"
  "https://github.com/timescale/tsbs.git"
  "https://github.com/emqx/emqtt-bench.git"
  "https://github.com/empicano/aiomqtt.git"
  "https://github.com/pmndrs/zustand.git"
  "https://github.com/redis/redis-py.git"
  "https://github.com/fastapi/full-stack-fastapi-template.git"
  "https://github.com/prometheus/client_python.git"
  "https://github.com/MagicStack/asyncpg.git"
  "https://github.com/hustcc/echarts-for-react.git"
  "https://github.com/Kludex/starlette.git"
  "https://github.com/sqlalchemy/alembic.git"
  "https://github.com/eclipse-paho/paho.mqtt.python.git"
)

ok=0; fail=0; failed_list=()
for url in "${REPOS[@]}"; do
  name="$(basename "$url" .git)"
  if [ -d "$name/.git" ]; then echo "SKIP  $name (already exists)"; ok=$((ok+1)); continue; fi
  if git clone --depth 1 --quiet "$url" "$name" 2>/dev/null; then
    echo "OK    $name"
    ok=$((ok+1))
  else
    echo "FAIL  $name  <- $url"
    fail=$((fail+1)); failed_list+=("$name")
  fi
done

# alertmanager: only take docs/ (full repo 57MB, sparse to ~816KB)
if [ -d "alertmanager/.git" ]; then
  echo "SKIP  alertmanager (already exists)"
else
  if git clone --depth 1 --quiet --filter=blob:none --sparse \
       https://github.com/prometheus/alertmanager.git alertmanager 2>/dev/null; then
    ( cd alertmanager && git sparse-checkout set docs >/dev/null 2>&1 )
    echo "OK    alertmanager (sparse: docs/)"
  else
    echo "FAIL  alertmanager"
    fail=$((fail+1)); failed_list+=("alertmanager")
  fi
fi

echo "-----"
echo "Succeeded/already present: $ok  Failed: $fail"
if [ "$fail" -gt 0 ]; then
  echo "Failed list: ${failed_list[*]}"
  echo "Hint: if it is a network issue (cannot reach github.com), report it truthfully; do not fabricate output."
  exit 1
fi
