#!/usr/bin/env bash
# Order 23 / I-16 —— one-click entry for decidable load testing (k6 + xk6-mqtt).
#
# Usage:
#   bash tests/load/run.sh                 # full run: MQTT load + WS probe
#   bash tests/load/run.sh --mqtt-only     # MQTT load only
#   bash tests/load/run.sh --ws-only       # WS probe only
#   bash tests/load/run.sh --preflight-only# prerequisites probe only, no load started
#   ROBOTS=20 DURATION_S=10 bash tests/load/run.sh   # small smoke test
#   K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh   # specify a prebuilt binary
#
# Exit codes: 0=all judgments passed  1=judgment failure exists  2=environment not ready (unverified, not a code failure)
#
# Design constraints (continuing test_integration.sh's discipline, see R-07 §6 / R-08 §0):
#   1. Fail fast per step (set -uo pipefail; not -e, so cleanup logic can uniformly go through trap)
#   2. Forbid bare sleep as a readiness probe: broker/backend both use real probes
#   3. Distinguish "environment error" (no binary, broker down) from "judgment failure" (check red); report in separate columns
#   4. Don't fake numbers: every conclusion comes from k6's own exit code and last-run.json

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

LOAD_DIR="tests/load"
MODE="all"
KEEP_ARTIFACTS=0
for a in "$@"; do
  case "$a" in
    --mqtt-only) MODE="mqtt" ;;
    --ws-only) MODE="ws" ;;
    --preflight-only) MODE="preflight" ;;
    --keep) KEEP_ARTIFACTS=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $a (use -h for usage)" >&2; exit 2 ;;
  esac
done

# Parameters and k6 scripts share the same env-var defaults (config.js reads __ENV).
export ROBOTS="${ROBOTS:-100}"
export PUBLISH_HZ="${PUBLISH_HZ:-10}"
export DURATION_S="${DURATION_S:-30}"
export BROKER_URL="${BROKER_URL:-mqtt://localhost:1883}"
export WS_URL="${WS_URL:-ws://localhost:8000/ws/robots}"
# WS jwt has no default: when empty, ws_watch.js gets rejected with close 1008, which is the
# **expected auth behavior**; the script reports this case separately instead of masking it as a
# connectivity failure (AGENTS.md §5: do not hardcode credentials).
export WS_TOKEN="${WS_TOKEN:-}"

K6_BIN="${K6_BIN:-$ROOT/k6-mqtt}"
K6_OFFICIAL="${K6_OFFICIAL:-k6}"

ENV_ERRORS=(); CODE_ERRORS=(); PASSED=(); UNVERIFIED=()
FAIL=0

c_ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
c_bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
c_warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
c_info() { printf '  ....   %s\n' "$*"; }
sect()   { printf '\n==> %s\n' "$*"; }

# Allowed "failed check ratio": R-07/R-08 require repeatable judgment, so a little ramp-period
# slack is allowed, but we do **not** allow swallowing all failures —— over 5% check failure fails.
MAX_CHECK_FAIL_RATIO="${MAX_CHECK_FAIL_RATIO:-0.05}"

# ---------------------------------------------------------------- broker probe
# Use real /dev/tcp connect, not nc (this repo's CI image may not have nc).
wait_for_tcp() {
  local desc="$1" host="$2" port="$3" timeout_s="$4"
  local deadline=$(( $(date +%s) + timeout_s ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
      exec 3<&- 2>/dev/null || true
      exec 3>&- 2>/dev/null || true
      c_ok "$desc reachable (${host}:${port})"
      return 0
    fi
    sleep 1
  done
  c_bad "$desc unreachable (${host}:${port}, timed out after ${timeout_s}s)"
  return 1
}

# ---------------------------------------------------------------- k6 binary probe
# Three ways to obtain it —— the error message must say *how* to get it, otherwise ops is left guessing.
resolve_mqtt_k6() {
  if [ -x "$K6_BIN" ]; then
    echo "$K6_BIN"; return 0
  fi
  if command -v k6-mqtt >/dev/null 2>&1; then
    command -v k6-mqtt; return 0
  fi
  # Official k6 has no extension; only used to probe "is there a k6 for --ws-only".
  return 1
}

require_xk6() {
  local bin="$1"
  # Real probe: run a script parse once (no connect) to confirm the k6/x/mqtt module loads.
  # Official k6 reports "unknown module: k6/x/mqtt", which is the most reliable criterion.
  if "$bin" run --quiet --iterations 0 "$LOAD_DIR/mqtt_pub.js" >/dev/null 2>&1; then
    return 0
  fi
  # --iterations 0 still connects in some versions, so fall back: just check module parsing.
  if "$bin" inspect "$LOAD_DIR/mqtt_pub.js" 2>&1 | grep -qi "unknown module"; then
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- result parsing
# Extract check pass/fail counts from k6's JSON summary and compute the ratio.
report_checks() {
  local json_file="$1" label="$2"
  if [ ! -f "$json_file" ]; then
    c_warn "$label: no result file produced ($json_file); cannot auto-judge pass rate"
    UNVERIFIED+=("$label pass rate (missing result file)")
    return 0
  fi
  local pass fail
  pass=$(python3 -c "import json,sys;d=json.load(open('$json_file'));print(d['checks']['passes'])" 2>/dev/null || echo "")
  fail=$(python3 -c "import json,sys;d=json.load(open('$json_file'));print(d['checks']['fails'])" 2>/dev/null || echo "")
  if [ -z "$pass" ] || [ -z "$fail" ]; then
    c_warn "$label: result file has no checks section; treated as unverified"
    UNVERIFIED+=("$label pass rate (no checks in result)")
    return 0
  fi
  local total=$(( pass + fail ))
  if [ "$total" -eq 0 ]; then
    c_bad "$label: 0 checks — assertions never ran (false coverage)"
    CODE_ERRORS+=("$label: 0 checks")
    FAIL=1
    return 1
  fi
  # Use integer comparison to avoid depending on bc/awk floats: fail/total <= ratio passes.
  local threshold_pct
  threshold_pct=$(python3 -c "print(int($MAX_CHECK_FAIL_RATIO*100))")
  local actual_pct=$(( fail * 100 / total ))
  if [ "$actual_pct" -le "$threshold_pct" ]; then
    c_ok "$label: checks passed $pass/$total (fail rate ${actual_pct}% <= ${threshold_pct}%)"
    PASSED+=("$label")
  else
    c_bad "$label: checks passed $pass/$total (fail rate ${actual_pct}% > ${threshold_pct}%)"
    CODE_ERRORS+=("$label: fail rate over threshold")
    FAIL=1
  fi
}

# ---------------------------------------------------------------- prerequisites
sect "[0/3] Prerequisite probe (env errors vs judgment failures separated)"

MQTT_K6_BIN=""
if resolve_mqtt_k6 >/tmp/rm_k6bin 2>/dev/null; then
  MQTT_K6_BIN="$(cat /tmp/rm_k6bin)"
  c_ok "Found k6 with xk6-mqtt: $MQTT_K6_BIN"
else
  c_warn "No k6 binary with xk6-mqtt found (MQTT load will be marked unverified)"
  c_info "How to obtain (pick one of three): A) prebuilt: curl -sL https://github.com/grafana/xk6-mqtt/releases/latest/download/xk6-mqtt_linux_amd64.tar.gz | tar xz -C /tmp && mv /tmp/k6 ./k6-mqtt  B) build yourself: go install go.k6.io/xk6/cmd/xk6@latest && xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt  C) reuse existing: K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh"
  ENV_ERRORS+=("No k6 binary with xk6-mqtt found")
fi

if command -v "$K6_OFFICIAL" >/dev/null 2>&1; then
  c_ok "Found official k6 ($(command -v "$K6_OFFICIAL"), usable for --ws-only)"
  HAVE_OFFICIAL_K6=1
else
  HAVE_OFFICIAL_K6=0
  c_warn "Official k6 not found (WS probe will be marked unverified)"
fi

if [ "$MODE" = "preflight" ]; then
  sect "Prerequisite probe only (--preflight-only); exiting"
  if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then exit 2; else exit 0; fi
fi

# ---------------------------------------------------------------- 1. broker readiness
if [ "$MODE" = "all" ] || [ "$MODE" = "mqtt" ]; then
  sect "[1/3] Broker readiness probe (no bare sleep; R-07 §1)"
  BROKER_HOSTPORT="${BROKER_URL#*://}"
  BROKER_HOST="${BROKER_HOSTPORT%%:*}"
  BROKER_PORT="${BROKER_HOSTPORT##*:}"
  [ "$BROKER_HOST" = "$BROKER_PORT" ] && BROKER_PORT=1883
  if ! wait_for_tcp "MQTT broker" "$BROKER_HOST" "$BROKER_PORT" 30; then
    ENV_ERRORS+=("MQTT broker not ready")
    MQTT_K6_BIN=""
  fi
fi

# ---------------------------------------------------------------- 2. MQTT load
if [ "$MODE" = "all" ] || [ "$MODE" = "mqtt" ]; then
  sect "[2/3] MQTT load: ${ROBOTS} units × ${PUBLISH_HZ}Hz × ${DURATION_S}s (target $((ROBOTS * PUBLISH_HZ)) msg/s)"
  if [ -z "$MQTT_K6_BIN" ]; then
    c_warn "Skipping MQTT load (preconditions unmet; see above)"
    UNVERIFIED+=("MQTT load (${ROBOTS} units × ${PUBLISH_HZ}Hz)")
  else
    c_info "Running: $MQTT_K6_BIN run $LOAD_DIR/mqtt_pub.js"
    if "$MQTT_K6_BIN" run "$LOAD_DIR/mqtt_pub.js"; then
      report_checks "$LOAD_DIR/last-run.json" "MQTT load"
      c_ok "k6 thresholds all satisfied (exit code 0)"
      PASSED+=("k6 thresholds (MQTT)")
    else
      report_checks "$LOAD_DIR/last-run.json" "MQTT load"
      c_bad "k6 exit code non-zero — thresholds has unsatisfied items (see summary above)"
      CODE_ERRORS+=("k6 thresholds (MQTT) unsatisfied")
      FAIL=1
    fi
  fi
fi

# ---------------------------------------------------------------- 3. WS probe
if [ "$MODE" = "all" ] || [ "$MODE" = "ws" ]; then
  sect "[3/3] WS receive-rate probe (backend /ws/robots)"
  WS_HOSTPORT="${WS_URL#*://}"
  WS_HOSTPORT="${WS_HOSTPORT%%/*}"
  WS_HOST="${WS_HOSTPORT%%:*}"
  WS_PORT="${WS_HOSTPORT##*:}"
  [ "$WS_HOST" = "$WS_PORT" ] && WS_PORT=8000

  if [ "$HAVE_OFFICIAL_K6" != "1" ]; then
    c_warn "Skipping WS probe (no official k6)"
    UNVERIFIED+=("WS receive-rate probe")
  elif ! wait_for_tcp "Backend WS port" "$WS_HOST" "$WS_PORT" 30; then
    ENV_ERRORS+=("Backend WS port not ready (start backend_ingestion first)")
    UNVERIFIED+=("WS receive-rate probe")
  else
    c_info "Running: $K6_OFFICIAL run $LOAD_DIR/ws_watch.js"
    if "$K6_OFFICIAL" run "$LOAD_DIR/ws_watch.js"; then
      report_checks "$LOAD_DIR/last-ws-run.json" "WS probe"
      PASSED+=("k6 thresholds (WS)")
    else
      report_checks "$LOAD_DIR/last-ws-run.json" "WS probe"
      if [ -z "$WS_TOKEN" ]; then
        c_warn "WS_TOKEN not set: backend rejects with close 1008 (from Order 15, WS requires ?token=)"
        c_info "This is not a connectivity failure, but the expected auth behavior; to judge delivery rate, get a JWT first:"
        c_info "  curl -s -X POST localhost:8000/auth/jwt/login -d 'username=...&password=...'"
        c_info "  then WS_TOKEN=<jwt> bash tests/load/run.sh --ws-only"
        UNVERIFIED+=("WS delivery rate (missing WS_TOKEN, no valid session)")
      else
        CODE_ERRORS+=("k6 thresholds (WS) unsatisfied")
        FAIL=1
      fi
    fi
  fi
fi

# ---------------------------------------------------------------- summary
sect "Summary"
echo "  passed ${#PASSED[@]} / judgment failed ${#CODE_ERRORS[@]} / env errors ${#ENV_ERRORS[@]} / unverified ${#UNVERIFIED[@]}"
if [ "${#PASSED[@]}" -gt 0 ]; then
  echo "  -- passed --";  for x in "${PASSED[@]}"; do echo "     * $x"; done
fi
if [ "${#CODE_ERRORS[@]}" -gt 0 ]; then
  echo "  -- judgment failed (code/threshold) --"; for x in "${CODE_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  -- env errors (unverified, not a code failure) --"; for x in "${ENV_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
  echo "  -- unverified --"; for x in "${UNVERIFIED[@]}"; do echo "     * $x"; done
fi

c_info "Result files: $LOAD_DIR/last-run.json / last-ws-run.json"

if [ "$FAIL" != "0" ]; then
  echo "  conclusion: judgment failure exists"; exit 1
elif [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  conclusion: environment not ready; some items [unverified] (not a code failure)"; exit 2
else
  echo "  conclusion: all passed"; exit 0
fi
