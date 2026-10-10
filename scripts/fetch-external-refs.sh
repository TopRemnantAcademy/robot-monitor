#!/usr/bin/env bash
# 拉取 external/ 下 14 个上游开源参考源。
# 来源与取舍依据见 external/SOURCES.md；套用点见 external/INTEGRATION-PLAN.md。
# 用法: bash scripts/fetch-external-refs.sh [目标目录，默认 external]
set -uo pipefail

DEST="${1:-external}"
mkdir -p "$DEST"
cd "$DEST"

# 全部为 GitHub 官方组织仓库，--depth 1 浅克隆
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
  if [ -d "$name/.git" ]; then echo "SKIP  $name (已存在)"; ok=$((ok+1)); continue; fi
  if git clone --depth 1 --quiet "$url" "$name" 2>/dev/null; then
    echo "OK    $name"
    ok=$((ok+1))
  else
    echo "FAIL  $name  <- $url"
    fail=$((fail+1)); failed_list+=("$name")
  fi
done

# alertmanager: 只取 docs/（全量 57MB，sparse 后 ~816KB）
if [ -d "alertmanager/.git" ]; then
  echo "SKIP  alertmanager (已存在)"
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
echo "成功/已存在: $ok  失败: $fail"
if [ "$fail" -gt 0 ]; then
  echo "失败清单: ${failed_list[*]}"
  echo "提示: 若为网络问题（无法访问 github.com），请如实上报，勿伪造产出。"
  exit 1
fi
