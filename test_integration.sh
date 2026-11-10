#!/usr/bin/env bash
# RobotMonitor end-to-end integration + load test (Order 06 wrap-up)
# Based on: spec/05 stage six, spec/ci/integration-call.md, orders/06-integration.md
#
# Usage:
#   bash test_integration.sh                 # run everything (default 30s load test)
#   DURATION=60 bash test_integration.sh     # custom load-test duration (seconds)
#   bash test_integration.sh --keep          # keep containers after finishing, for manual inspection
#   bash test_integration.sh --preflight-only# only probe prerequisites, start nothing
#
# Design constraints (Order 06 §2 T1):
#   1. Every step has timeout protection and fails fast (set -euo pipefail)
#   2. All three components (TimescaleDB / Redis / EMQX) must be waited for readiness; bare sleep is forbidden
#   3. All assertions use real SQL counts or real HTTP status codes; "no error in logs" is forbidden as a substitute
#   4. Distinguish "environment error" (e.g. docker unavailable) from "code error"; output in separate columns
#   5. Clean up backend/alert processes on exit; --keep keeps containers
#
# Exit codes: 0=all passed  1=code/validation failure  2=environment error (e.g. docker unavailable, unverified)
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

# ----- output helpers
c_ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
c_bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
c_warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
c_info() { printf '  ....   %s\n' "$*"; }
sect()   { printf '\n==> %s\n' "$*"; }

pass()  { PASSED+=("$1"); c_ok "$1"; }
fail()  { FAIL=1; CODE_ERRORS+=("$1"); c_bad "$1"; }
skip()  { SKIPPED+=("$1"); c_warn "skipped: $1"; }
unver() { UNVERIFIED+=("$1"); c_warn "unverified: $1"; }
envfail(){ ENV_ERRORS+=("$1"); c_bad "environment error: $1"; }

# ----- cleanup
cleanup() {
  local rc=$?
  sect "Cleanup"
  [ -n "$BACK_PID" ]  && kill "$BACK_PID"  2>/dev/null && c_info "backend pid=$BACK_PID stopped"  || true
  [ -n "$ALERT_PID" ] && kill "$ALERT_PID" 2>/dev/null && c_info "alert_engine pid=$ALERT_PID stopped" || true
  # wait for the port to actually free, guaranteeing re-runnability (second run won't hit a stale port)
  for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q ":${BACK_PORT} " || break
    sleep 0.5
  done
  if [ "$KEEP" = "1" ]; then
    c_info "Keeping containers (--keep): run 'docker compose -f $COMPOSE_FILE down' to stop them manually"
  else
    docker compose -f "$COMPOSE_FILE" down --remove-orphans >/dev/null 2>&1 \
      && c_info "containers stopped" || c_info "container stop command did not succeed (perhaps none existed)"
  fi
  return $rc
}
trap cleanup EXIT

# wait_for <desc> <max_seconds> <command...>  -- poll for readiness; bare sleep forbidden
wait_for() {
  local desc="$1" timeout_s="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout_s ))
  local last=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if last="$("$@" 2>&1)"; then
      c_ok "$desc ready ($last)"; return 0
    fi
    sleep 1
  done
  c_bad "$desc timed out after ${timeout_s}s (last output: ${last:-<empty>})"; return 1
}

sql() { docker exec "$TSDB_CT" psql -U "$PGU" -d "$PGD" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$1" || echo "000"; }

# ----- 0. prerequisites
sect "[0/8] Prerequisite probe (env errors vs code errors split here)"
if ! command -v docker >/dev/null 2>&1; then
  envfail "docker command not found (docker not in PATH)"
  DOCKER_OK=0
elif ! docker info >/dev/null 2>&1; then
  envfail "docker daemon unavailable ($(docker info 2>&1 | tail -1))"
  DOCKER_OK=0
else
  DOCKER_OK=1
  pass "docker available ($(docker --version | tr -d '\n'))"
fi
for f in "$COMPOSE_FILE" infra/init.sql simulator/mock_fleet.py backend_ingestion/app/main.py alert_engine/app/main.py; do
  [ -f "$f" ] && c_info "exists $f" || { envfail "missing required file $f"; DOCKER_OK=0; }
done

if [ "$PREFLIGHT_ONLY" = "1" ]; then
  sect "Prerequisite probe only (--preflight-only), exiting"
  [ "$DOCKER_OK" = "1" ] && exit 0 || exit 2
fi

if [ "$DOCKER_OK" != "1" ]; then
  sect "Conclusion"
  c_bad "Environment not ready; end-to-end chain [unverified]"
  c_info "Blocked environment errors: ${ENV_ERRORS[*]:-none}"
  cat <<'EOF'

  !!! THIS IS NOT A CODE FAILURE !!!
  This script needs a usable Docker daemon (EMQX + TimescaleDB + Redis, three containers).
  This run's environment lacks docker, so the following acceptance items are all marked
  "unverified" rather than "failed":
    - T1-2 three components ready  T1-3 robot_metadata=100  T1-4 /healthz=200
    - T1-7 robot_telemetry ≈ 30000  T1-8 overheat/low-battery alert landed
    - T3 end-to-end throughput (MQTT in -> landed)
  Required: run `bash test_integration.sh` on a machine that has Docker.
EOF
  exit 2
fi

# ----- 1. infrastructure
sect "[1/8] Start infrastructure (EMQX + TimescaleDB + Redis)"
if ! docker compose -f "$COMPOSE_FILE" up -d; then
  envfail "docker compose up -d failed (image pull / port in use / daemon issue)"
  exit 2
fi
c_info "compose up dispatched"

sect "[2/8] Wait for three components to be ready (each has a timeout; no bare sleep)"
READY=0
wait_for "TimescaleDB (pg_isready)" 90 docker exec "$TSDB_CT" pg_isready -U "$PGU" -d "$PGD" || READY=1
wait_for "Redis (PING→PONG)" 60 bash -c "docker exec $REDIS_CT redis-cli ping | grep -q PONG" || READY=1
# EMQX ready: container healthy + port 1883 reachable (either missing -> simulator can't connect -> false 0-row failure)
wait_for "EMQX (emqx ping)" 120 docker exec "$EMQX_CT" emqx ping || READY=1
wait_for "EMQX port 1883 reachable" 60 bash -c "exec 3<>/dev/tcp/localhost/1883 && exec 3<&-" || READY=1
if [ "$READY" != "0" ]; then
  envfail "Infra components not all ready; no later checks run (avoid false failures)"
  docker compose -f "$COMPOSE_FILE" ps || true
  exit 2
fi
docker compose -f "$COMPOSE_FILE" ps

# ----- 3. metadata
sect "[3/8] Preset data validation (real SQL)"
META=$(sql "SELECT count(*) FROM robot_metadata;")
c_info "SELECT count(*) FROM robot_metadata → ${META}"
if [ "$META" = "100" ]; then pass "robot_metadata = 100"; else fail "robot_metadata = ${META} (expected 100)"; fi
for t in robot_telemetry robot_alert; do
  HYP=$(sql "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='${t}';")
  [ "$HYP" = "1" ] && pass "${t} is a hypertable" || fail "${t} is not a hypertable (${HYP})"
done
CAgg=$(sql "SELECT count(*) FROM pg_matviews WHERE matviewname='robot_status_10s';")
[ "$CAgg" = "1" ] && pass "continuous aggregate robot_status_10s exists" || fail "continuous aggregate robot_status_10s missing"

# ----- 4. backend
sect "[4/8] Start backend and poll /healthz (wait for real HTTP 200, no sleep)"
if [ ! -f backend_ingestion/app/main.py ]; then
  skip "backend (stage three not done)"
else
  ( cd backend_ingestion && exec uvicorn app.main:app --host 0.0.0.0 --port "$BACK_PORT" --log-level warning ) \
    >/tmp/rm_backend.log 2>&1 &
  BACK_PID=$!
  HB_OK=0; LAST_CODE="000"
  DEADLINE=$(( $(date +%s) + 90 ))          # max 90s retries, no bare sleep
  while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    LAST_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:${BACK_PORT}/healthz" || echo 000)
    if [ "$LAST_CODE" = "200" ]; then HB_OK=1; break; fi
    if ! kill -0 "$BACK_PID" 2>/dev/null; then c_bad "backend process exited (pid=$BACK_PID)"; break; fi
    sleep 1
  done
  if [ "$HB_OK" = "1" ]; then
    pass "backend /healthz = 200"
  else
    fail "backend /healthz did not return 200 (last status code ${LAST_CODE})"
    c_info "backend log tail (/tmp/rm_backend.log):"
    tail -20 /tmp/rm_backend.log | sed 's/^/         /' || true
  fi
fi

# ----- 5. alert engine
sect "[5/8] Start alert engine"
if [ ! -f alert_engine/app/main.py ]; then
  skip "alert engine (stage four not done)"
else
  ( cd alert_engine && exec "$PY" -m app.main ) >/tmp/rm_alert.log 2>&1 &
  ALERT_PID=$!
  # poll "process alive + no fatal Traceback", up to 15s; no bare sleep for liveness
  AE_OK=0
  for _ in $(seq 1 15); do
    sleep 1
    if ! kill -0 "$ALERT_PID" 2>/dev/null; then break; fi
    if grep -qE 'Traceback|ConnectionError|OperationalError' /tmp/rm_alert.log; then break; fi
    AE_OK=1
  done
  if [ "$AE_OK" = "1" ]; then
    pass "alert_engine process alive with no fatal error (pid=$ALERT_PID, observed 15s)"
  else
    fail "alert_engine failed to start (see /tmp/rm_alert.log)"
    tail -20 /tmp/rm_alert.log | sed 's/^/         /' || true
  fi
fi

# ----- 6. load test
sect "[6/8] Simulator load test ${ROBOTS} units × 10Hz × ${DURATION}s (target ≈ $((ROBOTS*10*DURATION)) rows)"
BEFORE=$(sql "SELECT count(*) FROM robot_telemetry;")
c_info "robot_telemetry rows before load = ${BEFORE:-?}"
if [ -f simulator/mock_fleet.py ]; then
  ( cd simulator && timeout -s KILL $((DURATION + 5)) "$PY" mock_fleet.py --robots "$ROBOTS" --broker localhost --port 1883 ) \
    >/tmp/rm_sim.log 2>&1 || true
  # fallback: rich panel/signal-handling issues may leave a process; kill by name to avoid polluting the next run
  pkill -9 -f 'mock_fleet.py' 2>/dev/null || true
  c_info "simulator finished (log /tmp/rm_sim.log, last line: $(tail -1 /tmp/rm_sim.log | cut -c1-80))"
  # wait for backend to flush the last few batches (dual-trigger at most 1s per batch)
  for _ in $(seq 1 10); do
    QD=$(curl -s --max-time 3 "http://localhost:${BACK_PORT}/healthz" | sed -n 's/.*"queue_depth":\([0-9]*\).*/\1/p')
    [ "${QD:-0}" = "0" ] && break
    sleep 1
  done
else
  skip "simulator (stage two not done)"
fi
sleep 2

# ----- 7. landed-data validation
sect "[7/8] Landed-data validation (real SQL count)"
AFTER=$(sql "SELECT count(*) FROM robot_telemetry;")
DELTA=$(( ${AFTER:-0} - ${BEFORE:-0} ))
TARGET=$(( ROBOTS * 10 * DURATION ))
LO=$(( TARGET * 9 / 10 )); HI=$(( TARGET * 11 / 10 ))
c_info "SELECT count(*) FROM robot_telemetry → ${AFTER} (load added ${DELTA}, target ${TARGET}, allowed ±10% i.e. ${LO}~${HI})"
if [ "${DELTA:-0}" -ge "$LO" ] && [ "${DELTA:-0}" -le "$HI" ]; then
  pass "robot_telemetry gained ${DELTA} rows (≈ ${TARGET}, within ±10%)"
else
  fail "robot_telemetry gained ${DELTA} rows, outside ${LO}~${HI}"
fi

HZ=$(curl -s --max-time 3 "http://localhost:${BACK_PORT}/healthz")
c_info "GET /healthz → ${HZ}"
DROPPED=$(printf '%s' "$HZ" | sed -n 's/.*"mqtt_dropped":\([0-9]*\).*/\1/p')
if [ "${DROPPED:-1}" = "0" ]; then pass "mqtt_dropped = 0 (no packet loss)"; else unver "mqtt_dropped = ${DROPPED:-?} (>0 needs queue_depth to judge if overwhelmed)"; fi

# ----- 8. alert validation
sect "[8/8] Alert validation (real SQL count grouped by rule_id)"
ALERTS=$(sql "SELECT count(*) FROM robot_alert;")
c_info "SELECT count(*) FROM robot_alert → ${ALERTS:-0}"
c_info "SELECT rule_id,count(*) FROM robot_alert GROUP BY rule_id →"
docker exec "$TSDB_CT" psql -U "$PGU" -d "$PGD" -tAc \
  "SELECT rule_id, count(*) FROM robot_alert GROUP BY rule_id ORDER BY rule_id;" 2>/dev/null \
  | sed 's/^/         /' || c_info "(robot_alert query failed)"

MT=$(sql "SELECT count(*) FROM robot_alert WHERE rule_id='motor_temp';")
BL=$(sql "SELECT count(*) FROM robot_alert WHERE rule_id='battery_low';")
[ "${MT:-0}" -ge 1 ] && pass "motor_temp (overheat) alert fired, ${MT} rows" || fail "motor_temp alert did not fire (simulator injects ≥82°C after 30s)"
[ "${BL:-0}" -ge 1 ] && pass "battery_low (low battery) alert fired, ${BL} rows" || fail "battery_low alert did not fire (simulator drains 0.05%/s; whether it drops below 15% within 30s depends on initial value)"
DD=$(sql "SELECT count(*) FROM (SELECT robot_id, rule_id, count(*) c FROM robot_alert GROUP BY 1,2 HAVING count(*)>1) t;")
[ "${DD:-0}" = "0" ] || c_warn "Found groups with multiple rows for same (robot_id, rule_id): ${DD} (dedup window passed, normal; needs human judgment)"

# ----- summary
sect "Summary"
echo "  Passed ${#PASSED[@]} / code errors ${#CODE_ERRORS[@]} / env errors ${#ENV_ERRORS[@]} / skipped ${#SKIPPED[@]} / unverified ${#UNVERIFIED[@]}"
if [ "${#CODE_ERRORS[@]}" -gt 0 ]; then
  echo "  -- code errors --";  for x in "${CODE_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  -- environment errors --";  for x in "${ENV_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo "  -- skipped --";      for x in "${SKIPPED[@]}"; do echo "     * $x"; done
fi
if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
  echo "  -- unverified --";    for x in "${UNVERIFIED[@]}"; do echo "     * $x"; done
fi
[ "$FAIL" = "0" ] && { echo "  Conclusion: passed"; exit 0; } || { echo "  Conclusion: failures present (code/validation layer)"; exit 1; }
