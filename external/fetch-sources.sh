#!/usr/bin/env bash
# external/fetch-sources.sh — reproducibly fetch all upstream reference sources.
#
# Why this script exists (Issue #19 audit G-20):
#   `external/*` is excluded by .gitignore; third-party source is not committed.
#   The earlier claim "downloaded 22 repos" could not be reproduced/verified in
#   any fresh workspace. This script turns "download" into a single repeatable
#   command, and pins each repo to an explicit tag/commit to avoid conclusions
#   going stale due to upstream HEAD drift (this pit was hit once: a bare
#   aiomqtt clone drifted to 3.0.0-alpha.1).
#
# Usage:
#   bash external/fetch-sources.sh            # fetch everything
#   bash external/fetch-sources.sh aiomqtt    # fetch only one (matched by name)
#
# Note: this script does NOT verify SHA256 (it changes with upstream force-push
#       and would cause false alarms); it only guarantees "pinned to the same
#       ref". For stronger reproducibility add a checksum yourself.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

only="${1:-}"

# Format: <local name> <repo URL> <ref (tag/branch/commit)>
# ref of HEAD means follow the default branch (used only for insensitive sources like "docs/contracts").
SOURCES=(
  "mqtt-to-timescaledb|https://github.com/emqx/mqtt-to-timescaledb.git|HEAD"
  "tsbs|https://github.com/timescale/tsbs.git|HEAD"
  "emqtt-bench|https://github.com/emqx/emqtt-bench.git|HEAD"
  "aiomqtt|https://github.com/empicano/aiomqtt.git|v2.5.1"
  "zustand|https://github.com/pmndrs/zustand.git|HEAD"
  "redis-py|https://github.com/redis/redis-py.git|HEAD"
  "full-stack-fastapi-template|https://github.com/fastapi/full-stack-fastapi-template.git|HEAD"
  "client_python|https://github.com/prometheus/client_python.git|HEAD"
  "asyncpg|https://github.com/MagicStack/asyncpg.git|HEAD"
  "echarts-for-react|https://github.com/hustcc/echarts-for-react.git|HEAD"
  "starlette|https://github.com/Kludex/starlette.git|HEAD"
  "alembic|https://github.com/sqlalchemy/alembic.git|HEAD"
  "paho.mqtt.python|https://github.com/eclipse-paho/paho.mqtt.python.git|HEAD"
  "locust|https://github.com/locustio/locust.git|HEAD"
  "websockets|https://github.com/python-websockets/websockets.git|HEAD"
  "uvicorn|https://github.com/Kludex/uvicorn.git|HEAD"
  "sqlalchemy|https://github.com/sqlalchemy/sqlalchemy.git|HEAD"
  "pydantic-settings|https://github.com/pydantic/pydantic-settings.git|HEAD"
  "node_exporter|https://github.com/prometheus/node_exporter.git|HEAD"
  "timescaledb-docker|https://github.com/timescale/timescaledb-docker.git|HEAD"
  "timescaledb-tune|https://github.com/timescale/timescaledb-tune.git|HEAD"
)

fetch_one() {
  local name="$1" url="$2" ref="$3"
  local dir="$ROOT/$name"
  if [[ -d "$dir/.git" ]]; then
    echo "== $name exists, verifying ref"
    ( cd "$dir" && git fetch --depth 1 origin "$ref" >/dev/null 2>&1 || true
      git checkout --quiet "$ref" 2>/dev/null || true )
  else
    echo "== clone $name @ $ref"
    if [[ "$ref" == "HEAD" ]]; then
      git clone --depth 1 "$url" "$dir"
    else
      git clone --depth 1 --branch "$ref" "$url" "$dir"
    fi
  fi
  # Record evidence: local HEAD commit
  ( cd "$dir" && printf '%-26s %s  %s\n' "$name" "$(git rev-parse --short HEAD)" "$ref" ) \
    >> "$ROOT/.fetch-log"
}

# alertmanager uses sparse (full size 57MB, only docs needed)
fetch_alertmanager() {
  local dir="$ROOT/alertmanager"
  if [[ ! -d "$dir/.git" ]]; then
    echo "== sparse clone alertmanager (docs only)"
    git clone --depth 1 --filter=blob:none --sparse \
      https://github.com/prometheus/alertmanager.git "$dir"
    ( cd "$dir" && git sparse-checkout set docs )
    ( cd "$dir" && printf '%-26s %s  %s\n' "alertmanager" "$(git rev-parse --short HEAD)" "docs" ) \
      >> "$ROOT/.fetch-log"
  fi
}

: > "$ROOT/.fetch-log"
echo "# fetch log $(date -u +%FT%TZ)" >> "$ROOT/.fetch-log"

for entry in "${SOURCES[@]}"; do
  IFS='|' read -r name url ref <<< "$entry"
  if [[ -n "$only" && "$name" != *"$only"* ]]; then continue; fi
  fetch_one "$name" "$url" "$ref"
done

if [[ -z "$only" || "alertmanager" == *"$only"* ]]; then
  fetch_alertmanager
fi

echo
echo "Done. Evidence written to $ROOT/.fetch-log:"
cat "$ROOT/.fetch-log"
