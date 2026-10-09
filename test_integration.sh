#!/usr/bin/env bash
# RobotMonitor 端到端联调脚本（参考 spec/05 阶段六 + spec/ci/integration-call.md）
# 用法： bash test_integration.sh
#
# 修订：
#  - 补齐 EMQX(MQTT Broker) 就绪等待：否则模拟器连不上 Broker，写入 0 条 → 假失败
#  - PID 变量先初始化为空：旧版在 set -u 下若 alert_engine 尚未实现，ALERT_PID 未赋值会直接中止脚本
#  - 后端改为等待 /healthz 返回 200，而不是固定 sleep 5
#  - 模块尚未实现时跳过并明确标注，而不是崩溃
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="infra/docker-compose.yml"
DURATION="${DURATION:-30}"

BACK_PID=""      # 先初始化，避免 set -u 下未绑定变量
ALERT_PID=""

wait_for() { # wait_for <描述> <检查命令...>
  local desc="$1"; shift
  for i in $(seq 1 30); do
    if "$@" >/dev/null 2>&1; then
      echo "    [ok] $desc"; return 0
    fi
    sleep 2
  done
  echo "    [FAIL] $desc（超时）"; return 1
}

cleanup() {
  [ -n "$BACK_PID" ]  && kill "$BACK_PID"  2>/dev/null || true
  [ -n "$ALERT_PID" ] && kill "$ALERT_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> [1/6] 启动基础设施 (EMQX + TimescaleDB + Redis)"
docker compose -f "$COMPOSE_FILE" up -d

echo "==> [2/6] 等待三个组件就绪"
wait_for "TimescaleDB" docker exec robot-monitor-timescaledb pg_isready -U postgres -d robot_monitor
wait_for "Redis"       bash -c 'docker exec robot-monitor-redis redis-cli ping | grep -q PONG'
wait_for "EMQX (MQTT)" docker exec robot-monitor-emqx emqx ctl status

echo "==> [3/6] 预置数据校验"
docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -t -c \
  "SELECT count(*) FROM robot_metadata;" | tr -d ' \n' | sed 's/^/    robot_metadata 行数(应为100): /'; echo

echo "==> [4/6] 启动后端 (uvicorn :8000)"
if [ -f backend_ingestion/app/main.py ]; then
  ( cd backend_ingestion && uvicorn app.main:app --host 0.0.0.0 --port 8000 ) &
  BACK_PID=$!
  wait_for "后端 /healthz" bash -c 'curl -fsS http://localhost:8000/healthz'
else
  echo "    跳过：backend_ingestion 尚未实现（阶段三未完成）"
fi

echo "==> [5/6] 启动告警引擎"
if [ -f alert_engine/app/main.py ]; then
  ( cd alert_engine && python -m app.main ) &
  ALERT_PID=$!
  sleep 3
else
  echo "    跳过：alert_engine 尚未实现（阶段四未完成）"
fi

echo "==> [6/6] 运行模拟器压测 ${DURATION}s (100 台 @10Hz)"
if [ -f simulator/mock_fleet.py ]; then
  ( cd simulator && timeout $((DURATION + 2)) python mock_fleet.py --robots 100 --broker localhost --port 1883 ) || true
else
  echo "    跳过：simulator 尚未实现（阶段二未完成）"
fi

echo "==> 校验 TimescaleDB 写入"
docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -t -c \
  "SELECT count(*) FROM robot_telemetry;" | tr -d ' \n' | sed 's/^/    robot_telemetry 行数: /'; echo

docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -t -c \
  "SELECT rule_id, count(*) FROM robot_alert GROUP BY rule_id ORDER BY rule_id;" 2>/dev/null \
  | sed 's/^/    alert: /' || echo "    alert: (告警引擎未实现或未触发)"

echo "==> 完成。基础设施仍在运行，可手动 docker compose -f $COMPOSE_FILE down 关闭。"
