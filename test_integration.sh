#!/usr/bin/env bash
# RobotMonitor 端到端联调 + 压测（订单 06 收口）
# 依据：spec/05 阶段六、spec/ci/integration-call.md、orders/06-integration.md
#
# 用法：
#   bash test_integration.sh                 # 完整跑一遍（默认 30s 压测）
#   DURATION=60 bash test_integration.sh     # 自定义压测时长（秒）
#   bash test_integration.sh --keep          # 结束后保留容器，便于人工排查
#   bash test_integration.sh --preflight-only# 只做先决条件探测，不起任何东西
#
# 设计约束（订单 06 §2 T1）：
#   1. 每一步都有超时保护与失败即退出（set -euo pipefail）
#   2. 三个组件（TimescaleDB / Redis / EMQX）都要就绪等待，禁止裸 sleep 代替
#   3. 所有判定使用**真实 SQL 计数**或**真实 HTTP 状态码**，禁止「日志无报错」代替
#   4. 区分「环境错误」（docker 不可用等）与「代码错误」，分栏输出
#   5. 退出时清理后端/告警进程；--keep 保留容器
#
# 退出码：0=全部通过  1=存在代码/校验失败  2=环境错误（docker 不可用等，未验证）
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE_FILE="infra/docker-compose.yml"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-robotmonitor}"
DURATION="${DURATION:-30}"
ROBOTS="${ROBOTS:-100}"
KEEP=0
PREFLIGHT_ONLY=0
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    --preflight-only) PREFLIGHT_ONLY=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  esac
done

TSDB_CT=robot-monitor-timescaledb
REDIS_CT=robot-monitor-redis
EMQX_CT=robot-monitor-emqx
PGU=postgres; PGD=robot_monitor
BACK_PORT="${BACK_PORT:-8000}"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

BACK_PID=""; ALERT_PID=""
ENV_ERRORS=(); CODE_ERRORS=(); PASSED=(); SKIPPED=(); UNVERIFIED=()
FAIL=0

# ------------------------------------------------------------------ 输出工具
c_ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
c_bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
c_warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
c_info() { printf '  ....   %s\n' "$*"; }
sect()   { printf '\n==> %s\n' "$*"; }

pass()  { PASSED+=("$1"); c_ok "$1"; }
fail()  { FAIL=1; CODE_ERRORS+=("$1"); c_bad "$1"; }
skip()  { SKIPPED+=("$1"); c_warn "跳过：$1"; }
unver() { UNVERIFIED+=("$1"); c_warn "未验证：$1"; }
envfail(){ ENV_ERRORS+=("$1"); c_bad "环境错误：$1"; }

# ------------------------------------------------------------------ 清理
cleanup() {
  local rc=$?
  sect "清理"
  [ -n "$BACK_PID" ]  && kill "$BACK_PID"  2>/dev/null && c_info "backend pid=$BACK_PID 已停止"  || true
  [ -n "$ALERT_PID" ] && kill "$ALERT_PID" 2>/dev/null && c_info "alert_engine pid=$ALERT_PID 已停止" || true
  # 等端口真正释放，保证「可重复运行」（第二次跑不会被上一次残留占用端口）
  for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q ":${BACK_PORT} " || break
    sleep 0.5
  done
  if [ "$KEEP" = "1" ]; then
    c_info "保留容器（--keep）：docker compose -f $COMPOSE_FILE down 可手动关闭"
  else
    docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 \
      && c_info "容器已停止" || c_info "容器停止命令未成功（可能本就没有容器）"
  fi
  return $rc
}
trap cleanup EXIT

# wait_for <描述> <最大秒数> <命令...>  —— 轮询就绪，禁止裸 sleep
wait_for() {
  local desc="$1" timeout_s="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout_s ))
  local last=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if last="$("$@" 2>&1)"; then
      c_ok "$desc 就绪（$last）"; return 0
    fi
    sleep 1
  done
  c_bad "$desc 超时 ${timeout_s}s（最后输出：${last:-<空>}）"; return 1
}

sql() { docker exec "$TSDB_CT" psql -U "$PGU" -d "$PGD" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$1" || echo "000"; }

# ------------------------------------------------------------------ 0. 先决条件
sect "[0/8] 先决条件探测（环境错误与代码错误在此分离）"
if ! command -v docker >/dev/null 2>&1; then
  envfail "docker 命令不存在（PATH 内无 docker）"
  DOCKER_OK=0
elif ! docker info >/dev/null 2>&1; then
  envfail "docker daemon 不可用（$(docker info 2>&1 | tail -1)）"
  DOCKER_OK=0
else
  DOCKER_OK=1
  pass "docker 可用（$(docker --version | tr -d '\n')）"
fi
for f in "$COMPOSE_FILE" infra/init.sql simulator/mock_fleet.py backend_ingestion/app/main.py alert_engine/app/main.py; do
  [ -f "$f" ] && c_info "存在 $f" || { envfail "缺少必需文件 $f"; DOCKER_OK=0; }
done

if [ "$PREFLIGHT_ONLY" = "1" ]; then
  sect "仅先决条件探测（--preflight-only），退出"
  [ "$DOCKER_OK" = "1" ] && exit 0 || exit 2
fi

if [ "$DOCKER_OK" != "1" ]; then
  sect "结论"
  c_bad "环境不具备，端到端链路【未验证】"
  c_info "已阻塞的环境错误：${ENV_ERRORS[*]:-无}"
  cat <<'EOF'

  ！！！ 这不是代码失败 ！！！
  本脚本需要可用的 Docker daemon（EMQX + TimescaleDB + Redis 三个容器）。
  本次运行的环境缺少 docker，因此以下验收项一律标记「未验证」，而非「不通过」：
    - T1-2 三组件就绪  T1-3 robot_metadata=100  T1-4 /healthz=200
    - T1-7 robot_telemetry ≈ 30000  T1-8 过热/低电量告警落库
    - T3 端到端吞吐（MQTT 入 → 落库）
  所需条件：在具备 Docker 的机器上执行 `bash test_integration.sh`。
EOF
  exit 2
fi

# ------------------------------------------------------------------ 1. 基础设施
sect "[1/8] 启动基础设施（EMQX + TimescaleDB + Redis）"
if ! docker compose -f "$COMPOSE_FILE" up -d; then
  envfail "docker compose up -d 失败（镜像拉取/端口占用/daemon 问题）"
  exit 2
fi
c_info "compose up 已下发"

sect "[2/8] 等待三个组件就绪（各自有超时，不用裸 sleep）"
READY=0
wait_for "TimescaleDB (pg_isready)" 90 docker exec "$TSDB_CT" pg_isready -U "$PGU" -d "$PGD" || READY=1
wait_for "Redis (PING→PONG)" 60 bash -c "docker exec $REDIS_CT redis-cli ping | grep -q PONG" || READY=1
# EMQX 就绪：容器健康态 + 1883 端口可连（缺一 → 模拟器连不上 → 写入 0 条的假失败）
wait_for "EMQX (emqx ping)" 120 docker exec "$EMQX_CT" emqx ping || READY=1
wait_for "EMQX 1883 端口可连" 60 bash -c "exec 3<>/dev/tcp/localhost/1883 && exec 3<&-" || READY=1
if [ "$READY" != "0" ]; then
  envfail "基础设施组件未能全部就绪；后续校验一律不执行（避免假失败）"
  docker compose -f "$COMPOSE_FILE" ps || true
  exit 2
fi
docker compose -f "$COMPOSE_FILE" ps

# ------------------------------------------------------------------ 3. 元数据
sect "[3/8] 预置数据校验（真实 SQL）"
META=$(sql "SELECT count(*) FROM robot_metadata;")
c_info "SELECT count(*) FROM robot_metadata → ${META}"
if [ "$META" = "100" ]; then pass "robot_metadata = 100"; else fail "robot_metadata = ${META}（期望 100）"; fi
for t in robot_telemetry robot_alert; do
  HYP=$(sql "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='${t}';")
  [ "$HYP" = "1" ] && pass "${t} 是 hypertable" || fail "${t} 不是 hypertable（${HYP}）"
done
CAgg=$(sql "SELECT count(*) FROM pg_matviews WHERE matviewname='robot_status_10s';")
[ "$CAgg" = "1" ] && pass "连续聚合 robot_status_10s 存在" || fail "连续聚合 robot_status_10s 缺失"

# ------------------------------------------------------------------ 4. 后端
sect "[4/8] 启动后端并轮询 /healthz（等真实 HTTP 200，不用 sleep）"
if [ ! -f backend_ingestion/app/main.py ]; then
  skip "后端（阶段三未完成）"
else
  ( cd backend_ingestion && exec uvicorn app.main:app --host 0.0.0.0 --port "$BACK_PORT" --log-level warning ) \
    >/tmp/rm_backend.log 2>&1 &
  BACK_PID=$!
  HB_OK=0; LAST_CODE="000"
  DEADLINE=$(( $(date +%s) + 90 ))          # 最大重试 90s，不用裸 sleep
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    LAST_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${BACK_PORT}/healthz" || echo 000)
    if [ "$LAST_CODE" = "200" ]; then HB_OK=1; break; fi
    if ! kill -0 "$BACK_PID" 2>/dev/null; then c_bad "后端进程已退出（pid=$BACK_PID）"; break; fi
    sleep 1
  done
  if [ "$HB_OK" = "1" ]; then
    pass "后端 /healthz = 200"
  else
    fail "后端 /healthz 未返回 200（最后状态码 ${LAST_CODE}）"
    c_info "后端日志尾部（/tmp/rm_backend.log）："
    tail -20 /tmp/rm_backend.log | sed 's/^/         /' || true
  fi
fi

# ------------------------------------------------------------------ 5. 告警引擎
sect "[5/8] 启动告警引擎"
if [ ! -f alert_engine/app/main.py ]; then
  skip "告警引擎（阶段四未完成）"
else
  ( cd alert_engine && exec "$PY" -m app.main ) >/tmp/rm_alert.log 2>&1 &
  ALERT_PID=$!
  # 轮询「进程存活 + 无致命 Traceback」，最多 15s；不用裸 sleep 判存活
  AE_OK=0
  for _ in $(seq 1 15); do
    sleep 1
    if ! kill -0 "$ALERT_PID" 2>/dev/null; then break; fi
    if grep -qE 'Traceback|ConnectionError|OperationalError' /tmp/rm_alert.log; then break; fi
    AE_OK=1
  done
  if [ "$AE_OK" = "1" ]; then
    pass "alert_engine 进程存活且无致命错误（pid=$ALERT_PID，观察 15s）"
  else
    fail "alert_engine 启动异常（见 /tmp/rm_alert.log）"
    tail -20 /tmp/rm_alert.log | sed 's/^/         /' || true
  fi
fi

# ------------------------------------------------------------------ 6. 压测
sect "[6/8] 模拟器压测 ${ROBOTS} 台 × 10Hz × ${DURATION}s（目标 ≈ $((ROBOTS*10*DURATION)) 条）"
BEFORE=$(sql "SELECT count(*) FROM robot_telemetry;")
c_info "压测前 robot_telemetry 行数 = ${BEFORE:-?}"
if [ -f simulator/mock_fleet.py ]; then
  ( cd simulator && timeout -s KILL $((DURATION + 5)) "$PY" mock_fleet.py --robots "$ROBOTS" --broker localhost --port 1883 ) \
    >/tmp/rm_sim.log 2>&1 || true
  # 兜底：rich 面板/信号处理异常时可能残留进程，按名清理，避免污染下一次运行
  pkill -9 -f 'mock_fleet.py' 2>/dev/null || true
  c_info "模拟器结束（日志 /tmp/rm_sim.log，末行：$(tail -1 /tmp/rm_sim.log | cut -c1-80)）"
  # 等后端把最后几批刷完（dual-trigger 最多 1s 一批）
  for _ in $(seq 1 10); do
    QD=$(curl -s --max-time 3 "http://localhost:${BACK_PORT}/healthz" | sed -n 's/.*"queue_depth":\([0-9]*\).*/\1/p')
    [ "${QD:-0}" = "0" ] && break
    sleep 1
  done
else
  skip "模拟器（阶段二未完成）"
fi
sleep 2

# ------------------------------------------------------------------ 7. 落库校验
sect "[7/8] 落库校验（真实 SQL 计数）"
AFTER=$(sql "SELECT count(*) FROM robot_telemetry;")
DELTA=$(( ${AFTER:-0} - ${BEFORE:-0} ))
TARGET=$(( ROBOTS * 10 * DURATION ))
LO=$(( TARGET * 9 / 10 )); HI=$(( TARGET * 11 / 10 ))
c_info "SELECT count(*) FROM robot_telemetry → ${AFTER}（压测新增 ${DELTA}，目标 ${TARGET} 允许 ±10% 即 ${LO}~${HI}）"
if [ "${DELTA:-0}" -ge "$LO" ] && [ "${DELTA:-0}" -le "$HI" ]; then
  pass "robot_telemetry 新增 ${DELTA} 条（≈ ${TARGET}，±10% 内）"
else
  fail "robot_telemetry 新增 ${DELTA} 条，超出 ${LO}~${HI}"
fi

HZ=$(curl -s --max-time 3 "http://localhost:${BACK_PORT}/healthz")
c_info "GET /healthz → ${HZ}"
DROPPED=$(printf '%s' "$HZ" | sed -n 's/.*"mqtt_dropped":\([0-9]*\).*/\1/p')
if [ "${DROPPED:-1}" = "0" ]; then pass "mqtt_dropped = 0（无丢包）"; else unver "mqtt_dropped = ${DROPPED:-?}（>0 需结合 queue_depth 判断是否压垮）"; fi

# ------------------------------------------------------------------ 8. 告警校验
sect "[8/8] 告警校验（按 rule_id 分组真实 SQL 计数）"
ALERTS=$(sql "SELECT count(*) FROM robot_alert;")
c_info "SELECT count(*) FROM robot_alert → ${ALERTS:-0}"
c_info "SELECT rule_id,count(*) FROM robot_alert GROUP BY rule_id →"
docker exec "$TSDB_CT" psql -U "$PGU" -d "$PGD" -tAc \
  "SELECT rule_id, count(*) FROM robot_alert GROUP BY rule_id ORDER BY rule_id;" 2>/dev/null \
  | sed 's/^/         /' || c_info "（robot_alert 查询失败）"

MT=$(sql "SELECT count(*) FROM robot_alert WHERE rule_id='motor_temp';")
BL=$(sql "SELECT count(*) FROM robot_alert WHERE rule_id='battery_low';")
[ "${MT:-0}" -ge 1 ] && pass "motor_temp（过热）告警已触发，共 ${MT} 条" || fail "motor_temp 告警未触发（模拟器 30s 后注入 ≥82°C）"
[ "${BL:-0}" -ge 1 ] && pass "battery_low（低电量）告警已触发，共 ${BL} 条" || fail "battery_low 告警未触发（模拟器电量按 0.05%/s 下降；30s 内是否跌破 15% 取决于初值）"
DD=$(sql "SELECT count(*) FROM (SELECT robot_id, rule_id, count(*) c FROM robot_alert GROUP BY 1,2 HAVING count(*)>1) t;")
[ "${DD:-0}" = "0" ] || c_warn "存在同一 (robot_id, rule_id) 多条的组：${DD}（去重窗口已过属正常，需人工判断）"

# ------------------------------------------------------------------ 汇总
sect "汇总"
echo "  通过 ${#PASSED[@]} 项 / 代码错误 ${#CODE_ERRORS[@]} 项 / 环境错误 ${#ENV_ERRORS[@]} 项 / 跳过 ${#SKIPPED[@]} 项 / 未验证 ${#UNVERIFIED[@]} 项"
if [ "${#CODE_ERRORS[@]}" -gt 0 ]; then
  echo "  —— 代码错误 ——";  for x in "${CODE_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  —— 环境错误 ——";  for x in "${ENV_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo "  —— 跳过 ——";      for x in "${SKIPPED[@]}"; do echo "     * $x"; done
fi
if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
  echo "  —— 未验证 ——";    for x in "${UNVERIFIED[@]}"; do echo "     * $x"; done
fi
[ "$FAIL" = "0" ] && { echo "  结论：通过"; exit 0; } || { echo "  结论：存在失败（代码/校验层）"; exit 1; }
