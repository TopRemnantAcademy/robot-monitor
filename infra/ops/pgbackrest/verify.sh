#!/usr/bin/env bash
# pgBackRest 备份真实性校验（T2 三入口之一，**真断言，非 echo OK**）
#
# 三层校验，逐层依证据判定：
#   L1 仓库一致性：`pgbackrest verify --verbose` —— 逐文件校验和比对。
#         ⚠️ 实测陷阱：verify 检出 invalid checksum 时**退出码仍为 0**，
#            故本层解析输出文本（status: ok / status: valid / 无 invalid 计数），
#            而不是只判退出码 —— 只判退出码会在仓库损坏时给出假绿。
#   L2 备份新鲜度：解析 `info --output=json`，断言
#         - status.code == 0（stanza 健康）
#         - 至少存在一份 **全量** 备份（retention 的基准）
#         - 最新备份 error == false，且 timestamp.stop 在容忍窗口内
#         - WAL 归档区间非空（archive.min/max 均存在）
#   L3 恢复真实性（可选，最强证据）：把最新备份 **真实恢复** 到临时目录，
#         启动只读实例，比对源库与恢复库的 行数 / 最新时间戳。
#         需要 --scratch 参数；环境不具备时打印明确 skip 原因并以 0 退出。
#
# 用法：
#   verify.sh [--max-age-seconds N] [--scratch] [--help]
#
# 退出码：
#   0  校验通过（或 L3 因环境不具备而 skip —— 会写明缺什么条件）
#   1  校验失败（真实断言不成立）
#   2  用法错误
#   3  缺少前置条件（无 pgbackrest / 无 jq / stanza 不可用）
#
# 禁止 `|| true`：任一断言不成立即以非 0 退出，禁止静默失败（订单 31 §禁止项 2）。
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MAX_AGE="${PGBACKREST_MAX_BACKUP_AGE_SECONDS:-86400}"   # 默认 24h 内必须有一次成功备份
SCRATCH="no"

log()  { printf '[dr-verify] %s\n' "$*"; }
die()  { printf '[dr-verify] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }
fail() { printf '[dr-verify] FAIL: %s\n' "$*" >&2; FAILED=1; }
FAILED=0

usage() {
  cat <<'USAGE'
pgBackRest 备份真实性校验（真断言）

用法：
  verify.sh [--max-age-seconds N] [--scratch] [--help]

选项：
  --max-age-seconds N   最新成功备份距今超过 N 秒即判失败（默认 86400 = 24h）
  --scratch             追加 L3「恢复真实性」：真恢复到临时目录并比对行数/时间戳
  --help                本帮助

退出码：0 通过；1 断言失败；2 用法错误；3 前置条件缺失
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --max-age-seconds) MAX_AGE="${2:-}"; shift 2 ;;
    --scratch)         SCRATCH="yes"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) printf '[dr-verify] ERROR: 未知参数 %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MAX_AGE" in
  ''|*[!0-9]*) die "--max-age-seconds 必须是正整数，收到 '$MAX_AGE'" 2 ;;
esac

# ── 前置条件 ────────────────────────────────────────────────────────────
command -v pgbackrest >/dev/null 2>&1 || die "PATH 内找不到 pgbackrest 二进制" 3
command -v jq         >/dev/null 2>&1 || die "PATH 内找不到 jq（官方 user-guide.xml「Using jq」段推荐的 JSON 工具）" 3
command -v python3    >/dev/null 2>&1 || die "PATH 内找不到 python3（用于 L2 时间窗口计算）" 3

# stanza 必须存在且 info 可用 —— 否则属「前置条件缺失」（3），不是「断言不成立」（1）。
if ! pgbackrest --stanza="$STANZA" --output=json info >/dev/null 2>&1; then
  die "无法获取 stanza '$STANZA' 的 info（前置条件缺失）。先执行 backup.sh stanza-create" 3
fi
_hdr_code="$(pgbackrest --stanza="$STANZA" --output=json info 2>/dev/null | jq -r '.[0].status.code // empty')"
if [ "$_hdr_code" = "1" ]; then
  die "stanza '$STANZA' 不存在（status.code=1 missing stanza path）。先执行 backup.sh stanza-create" 3
fi

INFO_JSON="$(mktemp)"
trap 'rm -f "$INFO_JSON"' EXIT

# ── L1 仓库文件级一致性 ─────────────────────────────────────────────────
echo "== L1 仓库一致性（pgbackrest verify：逐文件校验和）=="
#
# ⚠️ 实测陷阱（pgBackRest 2.59.3）：`verify` 发现**校验和不符**时，
#    控制台会打印 `invalid checksum` 且 status: invalid，但**进程退出码仍为 0**！
#    —— 只判退出码 = 假绿。故必须同时把输出落盘解析（不能用 echo OK 式判据）。
VERIFY_OUT="$(mktemp)"
trap 'rm -f "$INFO_JSON" "$VERIFY_OUT"' EXIT
# ⚠️ 必须带 --verbose：不带时 verify 在**一切正常**的情况下什么都不打印，
#    只在出错时输出 —— 那样「无输出」既可能是成功也可能是被吞掉，无法判定。
set +e
pgbackrest --stanza="$STANZA" --log-level-console=info --output=text --verbose verify > "$VERIFY_OUT" 2>&1
verify_rc=$?
set -e

if [ "$verify_rc" -ne 0 ]; then
  fail "L1：pgbackrest verify 退出码 $verify_rc（命令级失败）"
  tail -20 "$VERIFY_OUT" >&2
fi

# 断言 1：stanza 段必须报告 `status: ok`（--verbose 下才会打印）
if grep -qE "^[[:space:]]*status: ok[[:space:]]*$" "$VERIFY_OUT"; then
  log "L1a OK：verify 报告 stanza status: ok"
else
  fail "L1a：verify 未报告 stanza status: ok"
  grep -E "status:" "$VERIFY_OUT" >&2 || true
fi

# 断言 2：每份备份必须 `status: valid`
backup_lines="$(grep -cE "^[[:space:]]*backup: .*status: valid" "$VERIFY_OUT" || true)"
if [ "${backup_lines:-0}" -ge 1 ]; then
  log "L1b OK：verify 逐份备份 status: valid（共 ${backup_lines} 份）"
else
  fail "L1b：没有任何备份被 verify 标为 status: valid"
  grep -E "backup: .*status:" "$VERIFY_OUT" >&2 || true
fi

# 断言 3：所有无效项计数必须为 0（invalid checksum / missing / size invalid / other）
if grep -qE "invalid checksum|status: invalid|missing: [1-9]|size invalid: [1-9]|other: [1-9]" "$VERIFY_OUT"; then
  fail "L1c：verify 检出无效文件 —— 仓库已损坏，备份不可用于恢复"
  grep -E "invalid checksum|status: invalid|missing:|size invalid:|other:" "$VERIFY_OUT" | sort -u >&2 || true
else
  log "L1c OK：无 invalid/missing/checksum/other 异常项"
fi

# ── 取 JSON（稳定接口）──
if ! pgbackrest --stanza="$STANZA" --output=json info > "$INFO_JSON" 2>/dev/null; then
  die "无法获取 stanza '$STANZA' 的 info JSON" 3
fi

# ── L2 备份新鲜度与完整性 ───────────────────────────────────────────────
echo
echo "== L2 备份新鲜度与完整性（断言 info JSON）=="

status_code="$(jq -r '.[0].status.code // empty' "$INFO_JSON")"
if [ "$status_code" = "0" ]; then
  log "L2a OK：stanza status.code=0（$(jq -r '.[0].status.message' "$INFO_JSON")）"
else
  fail "L2a：stanza 状态非 ok（status.code=${status_code:-空}，message=$(jq -r '.[0].status.message // "?"' "$INFO_JSON")）"
fi

backup_count="$(jq -r '.[0].backup | length' "$INFO_JSON")"
full_count="$(jq -r '[.[0].backup[] | select(.type=="full")] | length' "$INFO_JSON")"
if [ "${full_count:-0}" -ge 1 ]; then
  log "L2b OK：仓库内共 ${backup_count} 份备份，其中全量 ${full_count} 份（retention 基准存在）"
else
  fail "L2b：仓库内没有任何全量备份（full=0），retention 无基准"
fi

# 最新一份备份：error 必须为 false
if [ "${backup_count:-0}" -ge 1 ]; then
  latest_error="$(jq -r '.[0].backup[-1].error' "$INFO_JSON")"
  latest_label="$(jq -r '.[0].backup[-1].label' "$INFO_JSON")"
  latest_stop="$(jq -r '.[0].backup[-1].timestamp.stop' "$INFO_JSON")"
  if [ "$latest_error" = "false" ]; then
    log "L2c OK：最新备份 $latest_label 无错误（error=false）"
  else
    fail "L2c：最新备份 $latest_label 标记为 error=true（备份集不完整）"
  fi

  # 新鲜度：now - latest_stop <= MAX_AGE
  age="$(python3 -c "import sys,time; print(int(time.time()) - int(sys.argv[1]))" "$latest_stop")"
  if [ "$age" -le "$MAX_AGE" ]; then
    log "L2d OK：最新成功备份距今 ${age}s ≤ 阈值 ${MAX_AGE}s（stop=$(python3 -c "import sys,datetime;print(datetime.datetime.fromtimestamp(int(sys.argv[1])).isoformat())" "$latest_stop")）"
  else
    fail "L2d：最新成功备份距今 ${age}s > 阈值 ${MAX_AGE}s —— 备份已过期（RPO 失效）"
  fi
else
  fail "L2c/L2d：无备份可判定新鲜度"
fi

# WAL 归档区间非空（archive.min / archive.max 均存在）
arch_min="$(jq -r '.[0].archive[-1].min // empty' "$INFO_JSON")"
arch_max="$(jq -r '.[0].archive[-1].max // empty' "$INFO_JSON")"
if [ -n "$arch_min" ] && [ -n "$arch_max" ]; then
  log "L2e OK：WAL 归档区间非空 [${arch_min} .. ${arch_max}]（连续归档流已建立）"
else
  fail "L2e：WAL 归档区间为空（min='${arch_min}' max='${arch_max}'）—— archive_command 未生效"
fi

# ── L3 恢复真实性（可选，最强证据）──────────────────────────────────────
echo
if [ "$SCRATCH" != "yes" ]; then
  log "L3 SKIP：未指定 --scratch（默认不落盘恢复）。"
  log "           需要什么：--scratch + 一个可写的空目录 + 足够磁盘容纳最新备份解压体积；"
  log "           该模式会真实恢复并启动只读实例比对行数，见 docs/DR-RUNBOOK.md §4.6。"
else
  echo "== L3 恢复真实性（真恢复到临时目录 + 行数/时间戳比对）=="
  # 由独立脚本承载恢复比对，保持本脚本只做判定编排。
  if [ -x "$(dirname "$0")/verify-restore.sh" ]; then
    if "$(dirname "$0")/verify-restore.sh"; then
      log "L3 OK：恢复库与源库行数/最新时间戳一致"
    else
      fail "L3：恢复真实性比对失败"
    fi
  else
    fail "L3：--scratch 已指定，但找不到可执行的 verify-restore.sh"
  fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "[dr-verify] 全部断言通过（L1 + L2$([ "$SCRATCH" = yes ] && echo ' + L3')）。"
  exit 0
fi
echo "[dr-verify] 存在未通过断言，见上方 FAIL 行。" >&2
exit 1
