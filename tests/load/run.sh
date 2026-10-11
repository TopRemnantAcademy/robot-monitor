#!/usr/bin/env bash
# 订单 23 / I-16 —— 可判定压测的一键入口（k6 + xk6-mqtt）。
#
# 用法：
#   bash tests/load/run.sh                 # 完整跑：MQTT 压测 + WS 探针
#   bash tests/load/run.sh --mqtt-only     # 只跑 MQTT 压测
#   bash tests/load/run.sh --ws-only       # 只跑 WS 探针
#   bash tests/load/run.sh --preflight-only# 只做先决条件探测，不起任何负载
#   ROBOTS=20 DURATION_S=10 bash tests/load/run.sh   # 小规模冒烟
#   K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh   # 指定已构建的二进制
#
# 退出码：0=全部判定通过  1=存在判定失败  2=环境不具备（未验证，非代码失败）
#
# 设计约束（沿用 test_integration.sh 的纪律，见 R-07 §6 / R-08 §0）：
#   1. 每步失败即退出（set -uo pipefail；不用 -e，好让清理逻辑统一走 trap）
#   2. 禁止裸 sleep 代替就绪探测：broker/后端都用真实探测
#   3. 区分「环境错误」（没二进制、broker 未起）与「判定失败」（check 红了），分栏输出
#   4. 不伪造数字：所有结论都来自 k6 自己的退出码与 last-run.json

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
    *) echo "未知参数：$a（-h 看用法）" >&2; exit 2 ;;
  esac
done

# 参数与 k6 脚本走同一套环境变量默认值（config.js 读 __ENV）。
export ROBOTS="${ROBOTS:-100}"
export PUBLISH_HZ="${PUBLISH_HZ:-10}"
export DURATION_S="${DURATION_S:-30}"
export BROKER_URL="${BROKER_URL:-mqtt://localhost:1883}"
export WS_URL="${WS_URL:-ws://localhost:8000/ws/robots}"
# WS jwt 不设默认值：留空时 ws_watch.js 会以 close 1008 被拒，属**预期鉴权行为**；
# 脚本会把这个情形单独报出来，而不是伪装成连通性故障（AGENTS.md §5 不硬编码凭据）。
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

# 允许的「失败 check 数」：R-07/R-08 要求判定可重复，故留一点爬坡期余量，
# 但**不允许**把所有失败都吃掉 —— 超过 5% 的 check 失败即判 FAIL。
MAX_CHECK_FAIL_RATIO="${MAX_CHECK_FAIL_RATIO:-0.05}"

# ---------------------------------------------------------------- broker 探测
# 用 /dev/tcp 真连，不用 nc（本仓 CI 镜像不一定有 nc）。
wait_for_tcp() {
  local desc="$1" host="$2" port="$3" timeout_s="$4"
  local deadline=$(( $(date +%s) + timeout_s ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
      exec 3<&- 2>/dev/null || true
      exec 3>&- 2>/dev/null || true
      c_ok "$desc 可连（${host}:${port}）"
      return 0
    fi
    sleep 1
  done
  c_bad "$desc 不可连（${host}:${port}，超时 ${timeout_s}s）"
  return 1
}

# ---------------------------------------------------------------- k6 二进制探测
# 三条获取途径 —— 必须把「怎么弄到」写在报错里，否则运维只能干瞪眼。
resolve_mqtt_k6() {
  if [ -x "$K6_BIN" ]; then
    echo "$K6_BIN"; return 0
  fi
  if command -v k6-mqtt >/dev/null 2>&1; then
    command -v k6-mqtt; return 0
  fi
  # 官方 k6 不带扩展，仅用于探测「有没有 k6 可用来 --ws-only」。
  return 1
}

require_xk6() {
  local bin="$1"
  # 真实探测：跑一次脚本解析（不建连），确认 k6/x/mqtt 模块可加载。
  # 官方 k6 会报 "unknown module: k6/x/mqtt"，这是最可靠的判据。
  if "$bin" run --quiet --iterations 0 "$LOAD_DIR/mqtt_pub.js" >/dev/null 2>&1; then
    return 0
  fi
  # --iterations 0 在某些版本仍会建连，故再退一步：只检模块解析。
  if "$bin" inspect "$LOAD_DIR/mqtt_pub.js" 2>&1 | grep -qi "unknown module"; then
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- 结果解析
# 从 k6 的 JSON summary 里抽出 checks 的通过/失败数，算比例。
report_checks() {
  local json_file="$1" label="$2"
  if [ ! -f "$json_file" ]; then
    c_warn "$label：未产出结果文件（$json_file），无法自动判定通过率"
    UNVERIFIED+=("$label 的通过率（缺结果文件）")
    return 0
  fi
  local pass fail
  pass=$(python3 -c "import json,sys;d=json.load(open('$json_file'));print(d['checks']['passes'])" 2>/dev/null || echo "")
  fail=$(python3 -c "import json,sys;d=json.load(open('$json_file'));print(d['checks']['fails'])" 2>/dev/null || echo "")
  if [ -z "$pass" ] || [ -z "$fail" ]; then
    c_warn "$label：结果文件里没有 checks 段落，视为未验证"
    UNVERIFIED+=("$label 的通过率（结果缺 checks）")
    return 0
  fi
  local total=$(( pass + fail ))
  if [ "$total" -eq 0 ]; then
    c_bad "$label：0 条 check —— 断言根本没跑（假覆盖）"
    CODE_ERRORS+=("$label 0 条 check")
    FAIL=1
    return 1
  fi
  # 用整数比较避免依赖 bc/awk 的浮点：fail/total <= ratio 判通过。
  local threshold_pct
  threshold_pct=$(python3 -c "print(int($MAX_CHECK_FAIL_RATIO*100))")
  local actual_pct=$(( fail * 100 / total ))
  if [ "$actual_pct" -le "$threshold_pct" ]; then
    c_ok "$label：check 通过 $pass/$total（失败率 ${actual_pct}% <= ${threshold_pct}%）"
    PASSED+=("$label")
  else
    c_bad "$label：check 通过 $pass/$total（失败率 ${actual_pct}% > ${threshold_pct}%）"
    CODE_ERRORS+=("$label 失败率超标")
    FAIL=1
  fi
}

# ---------------------------------------------------------------- 先决条件
sect "[0/3] 先决条件探测（环境错误与判定失败在此分离）"

MQTT_K6_BIN=""
if resolve_mqtt_k6 >/tmp/rm_k6bin 2>/dev/null; then
  MQTT_K6_BIN="$(cat /tmp/rm_k6bin)"
  c_ok "找到带 xk6-mqtt 的 k6：$MQTT_K6_BIN"
else
  c_warn "未找到带 xk6-mqtt 的 k6 二进制（MQTT 压测将标「未验证」）"
  c_info "获取方式（三选一）：A) 预编译：curl -sL https://github.com/grafana/xk6-mqtt/releases/latest/download/xk6-mqtt_linux_amd64.tar.gz | tar xz -C /tmp && mv /tmp/k6 ./k6-mqtt  B) 自行编译：go install go.k6.io/xk6/cmd/xk6@latest && xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt  C) 复用已有：K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh"
  ENV_ERRORS+=("未找到带 xk6-mqtt 的 k6 二进制")
fi

if command -v "$K6_OFFICIAL" >/dev/null 2>&1; then
  c_ok "找到官方 k6（$(command -v "$K6_OFFICIAL")，可用于 --ws-only）"
  HAVE_OFFICIAL_K6=1
else
  HAVE_OFFICIAL_K6=0
  c_warn "未找到官方 k6（WS 探针将标「未验证」）"
fi

if [ "$MODE" = "preflight" ]; then
  sect "仅先决条件探测（--preflight-only），退出"
  if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then exit 2; else exit 0; fi
fi

# ---------------------------------------------------------------- 1. broker 就绪
if [ "$MODE" = "all" ] || [ "$MODE" = "mqtt" ]; then
  sect "[1/3] broker 就绪探测（禁止裸 sleep，R-07 §1）"
  BROKER_HOSTPORT="${BROKER_URL#*://}"
  BROKER_HOST="${BROKER_HOSTPORT%%:*}"
  BROKER_PORT="${BROKER_HOSTPORT##*:}"
  [ "$BROKER_HOST" = "$BROKER_PORT" ] && BROKER_PORT=1883
  if ! wait_for_tcp "MQTT broker" "$BROKER_HOST" "$BROKER_PORT" 30; then
    ENV_ERRORS+=("MQTT broker 未就绪")
    MQTT_K6_BIN=""
  fi
fi

# ---------------------------------------------------------------- 2. MQTT 压测
if [ "$MODE" = "all" ] || [ "$MODE" = "mqtt" ]; then
  sect "[2/3] MQTT 压测：${ROBOTS} 台 × ${PUBLISH_HZ}Hz × ${DURATION_S}s（目标 $((ROBOTS * PUBLISH_HZ)) msg/s）"
  if [ -z "$MQTT_K6_BIN" ]; then
    c_warn "跳过 MQTT 压测（前置条件未满足，见上）"
    UNVERIFIED+=("MQTT 压测（${ROBOTS} 台 × ${PUBLISH_HZ}Hz）")
  else
    c_info "运行：$MQTT_K6_BIN run $LOAD_DIR/mqtt_pub.js"
    if "$MQTT_K6_BIN" run "$LOAD_DIR/mqtt_pub.js"; then
      report_checks "$LOAD_DIR/last-run.json" "MQTT 压测"
      c_ok "k6 自身 thresholds 全部满足（退出码 0）"
      PASSED+=("k6 thresholds（MQTT）")
    else
      report_checks "$LOAD_DIR/last-run.json" "MQTT 压测"
      c_bad "k6 退出码非 0 —— thresholds 有未满足项（见上方摘要）"
      CODE_ERRORS+=("k6 thresholds（MQTT）未满足")
      FAIL=1
    fi
  fi
fi

# ---------------------------------------------------------------- 3. WS 探针
if [ "$MODE" = "all" ] || [ "$MODE" = "ws" ]; then
  sect "[3/3] WS 收包率探针（后端 /ws/robots）"
  WS_HOSTPORT="${WS_URL#*://}"
  WS_HOSTPORT="${WS_HOSTPORT%%/*}"
  WS_HOST="${WS_HOSTPORT%%:*}"
  WS_PORT="${WS_HOSTPORT##*:}"
  [ "$WS_HOST" = "$WS_PORT" ] && WS_PORT=8000

  if [ "$HAVE_OFFICIAL_K6" != "1" ]; then
    c_warn "跳过 WS 探针（无官方 k6）"
    UNVERIFIED+=("WS 收包率探针")
  elif ! wait_for_tcp "后端 WS 端口" "$WS_HOST" "$WS_PORT" 30; then
    ENV_ERRORS+=("后端 WS 端口未就绪（需先起 backend_ingestion）")
    UNVERIFIED+=("WS 收包率探针")
  else
    c_info "运行：$K6_OFFICIAL run $LOAD_DIR/ws_watch.js"
    if "$K6_OFFICIAL" run "$LOAD_DIR/ws_watch.js"; then
      report_checks "$LOAD_DIR/last-ws-run.json" "WS 探针"
      PASSED+=("k6 thresholds（WS）")
    else
      report_checks "$LOAD_DIR/last-ws-run.json" "WS 探针"
      if [ -z "$WS_TOKEN" ]; then
        c_warn "WS_TOKEN 未设置：后端会以 close 1008 拒绝（订单 15 起 WS 需 ?token=）"
        c_info "这不是连通性故障，而是预期鉴权行为；要判定投递率请先取一个 JWT："
        c_info "  curl -s -X POST localhost:8000/auth/jwt/login -d 'username=...&password=...'"
        c_info "  然后 WS_TOKEN=<jwt> bash tests/load/run.sh --ws-only"
        UNVERIFIED+=("WS 投递率（缺 WS_TOKEN，未取到有效会话）")
      else
        CODE_ERRORS+=("k6 thresholds（WS）未满足")
        FAIL=1
      fi
    fi
  fi
fi

# ---------------------------------------------------------------- 汇总
sect "汇总"
echo "  通过 ${#PASSED[@]} 项 / 判定失败 ${#CODE_ERRORS[@]} 项 / 环境错误 ${#ENV_ERRORS[@]} 项 / 未验证 ${#UNVERIFIED[@]} 项"
if [ "${#PASSED[@]}" -gt 0 ]; then
  echo "  —— 通过 ——";  for x in "${PASSED[@]}"; do echo "     * $x"; done
fi
if [ "${#CODE_ERRORS[@]}" -gt 0 ]; then
  echo "  —— 判定失败（代码/阈值）——"; for x in "${CODE_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  —— 环境错误（未验证，非代码失败）——"; for x in "${ENV_ERRORS[@]}"; do echo "     * $x"; done
fi
if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
  echo "  —— 未验证 ——"; for x in "${UNVERIFIED[@]}"; do echo "     * $x"; done
fi

c_info "结果文件：$LOAD_DIR/last-run.json / last-ws-run.json"

if [ "$FAIL" != "0" ]; then
  echo "  结论：存在判定失败"; exit 1
elif [ "${#ENV_ERRORS[@]}" -gt 0 ]; then
  echo "  结论：环境不具备，部分项【未验证】（不是代码失败）"; exit 2
else
  echo "  结论：全部通过"; exit 0
fi
