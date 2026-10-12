#!/usr/bin/env bash
# pgBackRest 备份入口（仓库(repository)侧边车内执行，T2 三入口之一）
#
# 用法：
#   backup.sh full        全量备份（建议每周日）
#   backup.sh diff        差异备份（建议周一~周六）
#   backup.sh incr        增量备份（可选，块级增量）
#   backup.sh check       配置/归档链路校验（stanza-create 之后先跑这条）
#   backup.sh info        打印仓库内现有备份清单（JSON，供 verify.sh 复用）
#   backup.sh --help      打印本帮助
#
# 契约（订单 31 §T2 与 §禁止项 2）：
#   - 幂等：重复执行 full/diff 不会损坏仓库；stanza-create 亦为幂等命令。
#   - 任何失败以**非 0**退出，且打印明确原因；**禁止** `|| true` / `continue-on-error`。
#   - 退出码：0=成功；2=用法错误；3=缺少前置条件（如 stanza 未建）；其余=pgbackrest 原样透传。
#
# 告警接入（订单 31 §T3，复用 #59 的既有通道，不另造一套）：
#   DB 侧 `monitor.pgbackrest_info()`（官方 user-guide.xml 的 Monitoring 示例）
#   + postgres_exporter 自定义查询 → `pgbackrest_backup_last_success_timestamp_seconds`
#   → `infra/observability/rules/robot-alerts.yml` 的 BackupStale 规则。
#   本脚本自身**不**推送告警：退出码即真值，由调度器/规则消费。
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"

log()  { printf '[dr-backup] %s\n' "$*"; }
die()  { printf '[dr-backup] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'USAGE'
pgBackRest 备份入口（仓库(repository)侧边车内执行，T2 三入口之一）

用法：
  backup.sh full        全量备份（建议每周日）
  backup.sh diff        差异备份（建议周一~周六）
  backup.sh incr        增量备份（可选，块级增量）
  backup.sh check       配置/归档链路校验（stanza-create 之后先跑这条）
  backup.sh info        打印仓库内现有备份清单（JSON，供 verify.sh 复用）
  backup.sh stanza-create  创建 stanza（幂等；首次部署必须先跑）
  backup.sh --help      打印本帮助

契约（订单 31 §T2 与 §禁止项 2）：
  - 幂等：重复执行 full/diff 不会损坏仓库；stanza-create 亦为幂等命令。
  - 任何失败以非 0 退出，且打印明确原因；禁止 `|| true` / `continue-on-error`。
  - 退出码：0=成功；2=用法错误；3=缺少前置条件（如 stanza 未建）；其余=pgbackrest 原样透传。

告警接入（订单 31 §T3，复用 #59 的既有通道，不另造一套）：
  DB 侧 monitor.pgbackrest_info()（官方 user-guide.xml 的 Monitoring 示例）
  + postgres_exporter 自定义查询 -> pgbackrest_backup_last_success_timestamp_seconds
  -> infra/observability/rules/robot-alerts.yml 的 BackupStale / BackupFailed 规则。
  本脚本自身不推送告警：退出码即真值，由调度器 / Prometheus 规则消费。
USAGE
}

MODE="${1:-}"
case "$MODE" in
  ""|-h|--help|help)
    usage
    [ -n "$MODE" ] || exit 2      # 无参数给帮助，但仍以用法错误退出，避免被当成成功
    exit 0
    ;;
esac

# 前置条件：pgbackrest 二进制必须在（DB 侧 vs 仓库侧装法不同，缺失时报清楚）
command -v pgbackrest >/dev/null 2>&1 \
  || die "PATH 内找不到 pgbackrest 二进制；仓库侧镜像见 Dockerfile，DB 侧见 Dockerfile.timescaledb" 3

# 前置条件：stanza 必须已创建。
#
# ⚠️ 坑（实测 2.59.3）：`pgbackrest --stanza=<不存在> info` **仍返回 0**，
#    只在 JSON 里报 `status: {"code": N, "message": "..."}`（如 "missing stanza path"）。
#    所以只判退出码会漏检，必须解析 JSON 的 status 字段。
if [ "$MODE" != "info" ] && [ "$MODE" != "check" ] && [ "$MODE" != "stanza-create" ]; then
  # jq 是官方 user-guide.xml「Using jq」段推荐的 JSON 提取工具（两个镜像均已装）。
  status_code="$(pgbackrest --stanza="$STANZA" --output=json info 2>/dev/null | jq -r '.[0].status.code // empty')"
  if [ "$status_code" != "0" ]; then
    die "stanza '$STANZA' 不可用（info status.code=${status_code:-空}）。先执行：backup.sh stanza-create" 3
  fi
fi

case "$MODE" in
  stanza-create)
    log "创建 stanza：$STANZA（幂等；已存在则跳过）"
    pgbackrest --stanza="$STANZA" --log-level-console=info stanza-create
    ;;
  check)
    log "校验配置与归档链路：$STANZA"
    # check 会触发一次 pg_switch_wal 并确认所需 WAL 已到仓库 —— 语义实证
    # external/pgbackrest/doc/xml/reference.xml check 命令段。
    pgbackrest --stanza="$STANZA" --log-level-console=info check
    ;;
  full|diff|incr)
    log "开始 ${MODE} 备份：stanza=$STANZA"
    # --type 取值 full/diff/incr 实证：reference.xml 的 backup 命令段。
    pgbackrest --stanza="$STANZA" --type="$MODE" --log-level-console=info backup
    log "${MODE} 备份完成"
    ;;
  info)
    # JSON 是**稳定接口**（reference.xml info 命令段："kept stable unless a bug is found"），
    # 故 verify.sh 与告警指标都消费它，而不是会随版本变动的 text 输出。
    pgbackrest --stanza="$STANZA" --output=json info
    ;;
  *)
    printf '[dr-backup] ERROR: 未知模式 %s\n' "$MODE" >&2
    printf '可用：full | diff | incr | check | info | stanza-create | --help\n' >&2
    exit 2
    ;;
esac
