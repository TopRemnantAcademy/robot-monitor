#!/usr/bin/env bash
# 下载上游开源参考源到 external/（供本地开发与 CNB 构建容器共用）。
#
# 为什么需要这个脚本：
#   external/ 下的**源码不入库**（.gitignore 排除，只保留 SOURCES.md 与 INTEGRATION-PLAN.md），
#   否则会把第三方代码与 license 灌进主仓、且体积随版本膨胀。
#   代价是 CNB 上的 NPC 读不到源码 —— 因此在需要对照上游实现时，
#   由本脚本在构建容器内重新拉取。
#
# 用法：
#   bash scripts/fetch-external-refs.sh            # 拉取全部
#   bash scripts/fetch-external-refs.sh aiomqtt    # 只拉指定源（可多个）
#
# 说明：
#   - 全部使用 --depth 1，只取最新提交，控制体积（全量约 59 MB）
#   - alertmanager 体积 57 MB，用 sparse checkout 只取 docs/（约 816 KB）
#   - 幂等：已存在的目录会跳过，重复执行安全

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p external

# 需求点 → 仓库 映射（与 external/SOURCES.md 保持一致）
# 格式："<目录名>|<仓库>|<需求点>"
ALL_REFS=(
  "mqtt-to-timescaledb|emqx/mqtt-to-timescaledb|MQTT Broker 部署契约"
  "tsbs|timescale/tsbs|时序库写入压测基线"
  "emqtt-bench|emqx/emqtt-bench|Broker 层吞吐压测工具"
  "aiomqtt|empicano/aiomqtt|MQTT 客户端 API 契约"
  "zustand|pmndrs/zustand|前端状态管理"
  "redis-py|redis/redis-py|异步 Redis 用法"
  "full-stack-fastapi-template|fastapi/full-stack-fastapi-template|全栈工程结构"
  "client_python|prometheus/client_python|指标暴露"
  "asyncpg|MagicStack/asyncpg|异步 PG 驱动（批量落库）"
  "echarts-for-react|hustcc/echarts-for-react|前端图表桥接"
  "starlette|Kludex/starlette|WebSocket 底层实现"
  "alembic|sqlalchemy/alembic|DB 迁移"
  "paho.mqtt.python|eclipse-paho/paho.mqtt.python|MQTT 底层客户端"
)

# alertmanager 单独处理（sparse）—— 不放在上面的循环里
ALERTMANAGER_DIR="alertmanager"
ALERTMANAGER_REPO="prometheus/alertmanager"

clone_one() {
  local dir="$1" repo="$2" purpose="$3"
  if [[ -d "external/${dir}/.git" ]]; then
    echo "  [skip] ${dir} (已存在)"
    return 0
  fi
  echo "  [get ] ${dir}  <- ${repo}   # ${purpose}"
  git clone --depth 1 -q "https://github.com/${repo}.git" "external/${dir}"
}

# ---- 参数：不传则拉全部；传了就只拉匹配的目录名 ----
SELECTED=("$@")

if [[ ${#SELECTED[@]} -eq 0 ]]; then
  echo "拉取全部 ${#ALL_REFS[@]} 个参考源 + alertmanager(docs)..."
  for entry in "${ALL_REFS[@]}"; do
    IFS='|' read -r dir repo purpose <<< "$entry"
    clone_one "$dir" "$repo" "$purpose"
  done

  if [[ -d "external/${ALERTMANAGER_DIR}/.git" ]]; then
    echo "  [skip] ${ALERTMANAGER_DIR} (已存在)"
  else
    echo "  [get ] ${ALERTMANAGER_DIR}  <- ${ALERTMANAGER_REPO}   # 告警设计标准（仅 docs/）"
    git clone --depth 1 --filter=blob:none --sparse -q \
      "https://github.com/${ALERTMANAGER_REPO}.git" "external/${ALERTMANAGER_DIR}"
    ( cd "external/${ALERTMANAGER_DIR}" && git sparse-checkout set docs )
  fi
else
  echo "拉取指定参考源: ${SELECTED[*]}"
  for want in "${SELECTED[@]}"; do
    if [[ "$want" == "${ALERTMANAGER_DIR}" ]]; then
      if [[ -d "external/${ALERTMANAGER_DIR}/.git" ]]; then
        echo "  [skip] ${ALERTMANAGER_DIR} (已存在)"
      else
        git clone --depth 1 --filter=blob:none --sparse -q \
          "https://github.com/${ALERTMANAGER_REPO}.git" "external/${ALERTMANAGER_DIR}"
        ( cd "external/${ALERTMANAGER_DIR}" && git sparse-checkout set docs )
        echo "  [get ] ${ALERTMANAGER_DIR}"
      fi
      continue
    fi
    found=0
    for entry in "${ALL_REFS[@]}"; do
      IFS='|' read -r dir repo purpose <<< "$entry"
      if [[ "$dir" == "$want" ]]; then
        clone_one "$dir" "$repo" "$purpose"
        found=1
        break
      fi
    done
    if [[ $found -eq 0 ]]; then
      echo "  [warn] 未知参考源: ${want}（跳过）" >&2
    fi
  done
fi

echo
echo "完成。external/ 当前内容："
du -sh external/*/ 2>/dev/null | sort -h
echo
echo "注意：external/ 下的源码**不入库**（.gitignore），仅 SOURCES.md 与 INTEGRATION-PLAN.md 入库。"
