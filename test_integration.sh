#!/usr/bin/env bash
# RobotMonitor 端到端联调脚本（spec/05 阶段六 + spec/ci/integration-call.md + R-07）
#
# 用法：
#   bash test_integration.sh                 # 默认压测 30s
#   DURATION=60 bash test_integration.sh     # 自定义压测时长
#   KEEP=1 bash test_integration.sh          # 结束后保留容器，便于人工排查（R-07 §7）
#
# 修订（2026-10-10 第二轮）：
#  - 就绪判据改用 `docker compose ps --format json` 读真实健康态，
#    而不是 `docker exec`（exec 绕开容器 healthcheck，测的不是 compose 声明的判据）
#  - 就绪等待失败时打印 `compose ps` + 各容器末 30 行日志，便于区分环境/代码错误
#  - 补 `KEEP` 开关与可重复运行保障（每次 up 之前不 down，靠 healthcheck 幂等；
#    结束时按 KEEP 决定是否 down）
#  - EMQX 就绪同时校验 1883 可连（R-07 §1 的三重判据：端口 + 健康态）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="infra/docker-compose.yml"
DURATION="${DURATION:-30}"
KEEP="${KEEP:-0}"

BACK_PID=""
ALERT_PID=""

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

# 单个容器的健康态：healthy / starting / unhealthy / "" (无 healthcheck)
health_of() {
  # 兼容 docker compose v2 的逐行 JSON 与旧版的整体 JSON 数组两种输出形态。
  # 只容忍「解析不出」的情形（返回空），绝不让解析异常冒泡到 set -e。
  dc ps --format json 2>/dev/null \
    | python3 -c "
import json, sys


def flatten(obj, out):
    # 逐层展开：既接受逐行 JSON 对象，也接受整体 JSON 数组
    # （实测坑：单行 JSON 数组会被 json.loads 解成 list，必须摊平，
    #   否则 records[0] 是 list，.get() 会 AttributeError 打断 set -e 脚本）
    if isinstance(obj, list):
        for item in obj:
            flatten(item, out)
    elif isinstance(obj, dict):
        out.append(obj)


name = sys.argv[1]
records = []
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        flatten(json.loads(line), records)
    except json.JSONDecodeError:
        pass
for o in records:
    if o.get('Service') == name or o.get('Name') == name:
        print(o.get('Health') or '')
        break
" "$1"
}

dump_diagnostics() {
  echo "    ---- 诊断：compose ps ----"
  dc ps || true
  for c in robot-monitor-emqx robot-monitor-timescaledb robot-monitor-redis; do
    echo "    ---- $c 末 30 行日志 ----"
    docker logs --tail 30 "$c" 2>&1 | sed 's/^/      /' || echo "      (容器不存在)"
  done
}

# wait_healthy <service> <描述> <尝试次数>
wait_healthy() {
  local svc="$1" desc="$2" tries="${3:-45}" i h
  for i in $(seq 1 "$tries"); do
    h="$(health_of "$svc" 2>/dev/null || true)"
    if [ "$h" = "healthy" ]; then
      echo "    [ok] $desc (healthy)"
      return 0
    fi
    sleep 2
  done
  echo "    [FAIL] $desc：${tries} 次探测后仍未 healthy（实际 health='${h:-<空>}'）"
  dump_diagnostics
  return 1
}

# wait_cmd <描述> <尝试次数> <命令...>：命令返回 0 即通过
wait_cmd() {
  local desc="$1" tries="$2"; shift 2
  local i
  for i in $(seq 1 "$tries"); do
    if "$@" >/dev/null 2>&1; then
      echo "    [ok] $desc"; return 0
    fi
    sleep 2
  done
  echo "    [FAIL] $desc（${tries} 次探测超时）"
  return 1
}

cleanup() {
  [ -n "$BACK_PID" ]  && kill "$BACK_PID"  2>/dev/null || true
  [ -n "$ALERT_PID" ] && kill "$ALERT_PID" 2>/dev/null || true
  if [ "$KEEP" = "1" ]; then
    echo "==> KEEP=1：容器保留，可手动排查。关闭：docker compose -f $COMPOSE_FILE down"
  fi
}
trap cleanup EXIT

# 报告分栏累加器（R-07 §4 要求 7 分栏 + 环境/代码错误分列）
ENV_ERRORS=""
CODE_ERRORS=""

note_env()  { ENV_ERRORS="${ENV_ERRORS}\n  - $1"; }
note_code() { CODE_ERRORS="${CODE_ERRORS}\n  - $1"; }

echo "==================================================================="
echo " RobotMonitor 端到端联调（DURATION=${DURATION}s, KEEP=${KEEP}）"
echo "==================================================================="

echo "==> [1/7] 启动基础设施 (EMQX + TimescaleDB + Redis)"
dc up -d

echo "==> [2/7] 等待三个组件 healthy（判据取 compose healthcheck 真实健康态）"
wait_healthy emqx        "EMQX"        || note_env "EMQX 未在超时内 healthy（镜像拉取慢/资源不足）"
wait_healthy timescaledb "TimescaleDB" || note_env "TimescaleDB 未在超时内 healthy"
wait_healthy redis       "Redis"       || note_env "Redis 未在超时内 healthy"

echo "==> [3/7] 连通性复核（端口可达，与健康态互补）"
wait_cmd "MQTT 1883 可连" 15 bash -c 'exec 3<>/dev/tcp/127.0.0.1/1883' 2>/dev/null \
  || note_env "MQTT 1883 端口不可连（EMQX 慢启动或端口占用）"
wait_cmd "TimescaleDB pg_isready" 15 \
  docker exec robot-monitor-timescaledb pg_isready -U postgres -d robot_monitor \
  || note_env "pg_isready 失败"
wait_cmd "Redis ping" 15 \
  bash -c 'docker exec robot-monitor-redis redis-cli ping | grep -q PONG' \
  || note_env "redis-cli ping 失败"

echo "==> [4/7] 预置数据校验（真实 SQL 计数，R-07 §2）"
META_COUNT="$(docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -tAc \
  'SELECT count(*) FROM robot_metadata;' 2>/dev/null | tr -d ' ' || echo '')"
echo "    robot_metadata 行数（应为 100）: ${META_COUNT:-<查询失败>}"
if [ "${META_COUNT:-X}" != "100" ]; then
  note_code "robot_metadata 行数=${META_COUNT:-查询失败}，期望 100（init.sql 播种未生效）"
fi

echo "==> [5/7] 启动后端 (uvicorn :8000)"
if [ -f backend_ingestion/app/main.py ]; then
  ( cd backend_ingestion && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      >/tmp/backend.log 2>&1 ) &
  BACK_PID=$!
  if wait_cmd "后端 /healthz = 200" 30 curl -fsS http://localhost:8000/healthz; then
    :
  else
    echo "    backend 末 30 行日志："
    tail -30 /tmp/backend.log 2>/dev/null | sed 's/^/      /'
    note_code "后端 /healthz 未在超时内返回 200（见 /tmp/backend.log）"
  fi
else
  echo "    跳过：backend_ingestion 尚未实现（阶段三未完成）"
fi

echo "==> [6/7] 启动告警引擎"
if [ -f alert_engine/app/main.py ]; then
  ( cd alert_engine && exec python -m app.main >/tmp/alert.log 2>&1 ) &
  ALERT_PID=$!
  sleep 3
  if ! kill -0 "$ALERT_PID" 2>/dev/null; then
    echo "    alert_engine 末 30 行日志："
    tail -30 /tmp/alert.log 2>/dev/null | sed 's/^/      /'
    note_code "alert_engine 进程启动后立即退出（见 /tmp/alert.log）"
    ALERT_PID=""
  fi
else
  echo "    跳过：alert_engine 尚未实现（阶段四未完成）"
fi

echo "==> [7/7] 运行模拟器压测 ${DURATION}s (100 台 @10Hz)"
if [ -f simulator/mock_fleet.py ]; then
  ( cd simulator && timeout $((DURATION + 5)) python mock_fleet.py \
      --robots 100 --broker localhost --port 1883 ) || true
else
  echo "    跳过：simulator 尚未实现（阶段二未完成）"
fi

# 留 2s 让 batch_writer 的 1s 定时触发把最后一批刷完
sleep 2

echo
echo "=================== 结果核验（真实 SQL，非日志推断）==================="
TELE_COUNT="$(docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -tAc \
  'SELECT count(*) FROM robot_telemetry;' 2>/dev/null | tr -d ' ' || echo '')"
echo "    robot_telemetry 行数: ${TELE_COUNT:-<查询失败>}"
echo "    （30s × 100 台 × 10Hz ≈ 30000；±10% 内视为正常，R-07 §2）"

echo
echo "    告警分布（按 rule_id）："
docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -tAc \
  'SELECT rule_id, count(*) FROM robot_alert GROUP BY rule_id ORDER BY rule_id;' 2>/dev/null \
  | sed 's/^/      /' || echo "      (robot_alert 不可查：告警引擎未实现或未触发)"

if [ -n "$BACK_PID" ] && kill -0 "$BACK_PID" 2>/dev/null; then
  echo
  echo "    后端 /healthz 快照："
  curl -fsS http://localhost:8000/healthz 2>/dev/null | sed 's/^/      /' || echo "      (不可达)"
fi

echo
echo "=================== 分栏结论（R-07 §4）==================="
echo "[1] 基础设施：$( [ -n "$ENV_ERRORS" ] && echo '有环境问题，见下' || echo '三组件 healthy 且端口可达' )"
echo "[2] 后端：$( [ -n "$BACK_PID" ] && echo '已启动（日志 /tmp/backend.log）' || echo '未实现/未启动' )"
echo "[3] 前端：未由本脚本覆盖（见 frontend_dashboard/.e2e/）"
echo "[4] 压测：DURATION=${DURATION}s，100 台 @10Hz"
echo "[5] telemetry 行数：${TELE_COUNT:-查询失败}"
echo "[6] alert 行数：见上方按 rule_id 分布"
echo "[7] 结论：$( [ -z "$ENV_ERRORS$CODE_ERRORS" ] && echo '全部分栏无错误' || echo '存在未通过项，见下' )"

echo
echo "--- 环境错误（不得归因于代码，R-07 §4）---"
if [ -n "$ENV_ERRORS" ]; then printf '%b\n' "$ENV_ERRORS"; else echo "  （无）"; fi
echo "--- 代码错误 ---"
if [ -n "$CODE_ERRORS" ]; then printf '%b\n' "$CODE_ERRORS"; else echo "  （无）"; fi

echo
echo "==> 完成。"
