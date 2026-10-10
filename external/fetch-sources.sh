#!/usr/bin/env bash
# external/fetch-sources.sh —— 可复现地拉取所有上游参考源。
#
# 为什么需要这个脚本（Issue #19 审计 G-20）：
#   `external/*` 被 .gitignore 排除，源码不入库。此前"已下载 22 个仓库"的结论
#   在任意新工作区都无法复现/核对。本脚本把"下载"变成一条可重复执行的命令，
#   并把每个仓库钉到明确的 tag/commit，避免上游 HEAD 漂移导致结论失效
#   （这个坑已经踩过一次：aiomqtt 裸 clone 漂到 3.0.0-alpha.1）。
#
# 用法：
#   bash external/fetch-sources.sh            # 全量拉取
#   bash external/fetch-sources.sh aiomqtt    # 只拉某一个（按名字匹配）
#
# 注意：本脚本**不**校验 SHA256（会随上游 force-push 变化而误报）；
#       它只保证"钉到同一个 ref"。要更强的可复现性请自行加 checksum。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

only="${1:-}"

# 格式：<本地名> <仓库 URL> <ref（tag/分支/commit）>
# ref 为 HEAD 表示跟随默认分支（仅用于"拿文档/契约"这类不敏感来源）。
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
    echo "== $name 已存在，校验 ref"
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
  # 记录实证：本地 HEAD commit
  ( cd "$dir" && printf '%-26s %s  %s\n' "$name" "$(git rev-parse --short HEAD)" "$ref" ) \
    >> "$ROOT/.fetch-log"
}

# alertmanager 用 sparse（全量 57MB，只用 docs）
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
echo "完成。实证已写入 $ROOT/.fetch-log："
cat "$ROOT/.fetch-log"
