#!/usr/bin/env bash
# RobotMonitor 端到端联调脚本（参考 spec/05 阶段四 + spec/ci/integration-call.md）
# 用法： bash test_integration.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> [1/6] 启动基础设施 (EMQX + TimescaleDB + Redis)"
docker compose -f infra/docker-compose.yml up -d

echo "==> [2/6] 等待 TimescaleDB 就绪"
for i in $(seq 1 30); do
  if docker exec robot-monitor-timescaledb pg_isready -U postgres -d robot_monitor >/dev/null 2>&1; then
    echo "    TimescaleDB ready"; break
  fi
  sleep 2
done

echo "==> [3/6] 等待 Redis 就绪"
for i in $(seq 1 30); do
  if docker exec robot-monitor-redis redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "    Redis ready"; break
  fi
  sleep 2
done

echo "==> [4/6] 启动后端 (uvicorn :8000)"
( cd backend_ingestion && uvicorn app.main:app --host 0.0.0.0 --port 8000 ) &
BACK_PID=$!
sleep 5

echo "==> [5/6] 启动告警引擎"
if [ -d alert_engine ]; then
  ( cd alert_engine && python -m app.main ) &
  ALERT_PID=$!
  sleep 3
fi

echo "==> [6/6] 运行模拟器压测 30s (100 台 @10Hz)"
( cd simulator && timeout 32 python mock_fleet.py --robots 100 --broker localhost --port 1883 ) || true

echo "==> 校验 TimescaleDB 写入"
docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -t -c \
  "SELECT count(*) FROM robot_telemetry;" | tr -d ' \n' | sed 's/^/    robot_telemetry 行数: /'; echo

docker exec robot-monitor-timescaledb psql -U postgres -d robot_monitor -t -c \
  "SELECT rule_id, count(*) FROM robot_alert GROUP BY rule_id;" 2>/dev/null | sed 's/^/    alert: /' || echo "    alert: (告警引擎未实现或未触发)"

kill $BACK_PID 2>/dev/null || true
kill $ALERT_PID 2>/dev/null || true
echo "==> 完成。基础设施仍在运行，可手动 docker compose -f infra/docker-compose.yml down 关闭。"
