#!/usr/bin/env bash
# 灾备调度入口：定期全量 + WAL 归档（在 pgbackrest 仓库边车内执行）
#
# 用法（从宿主机触发）：
#   docker compose -f infra/docker-compose.yml \
#                  -f infra/ops/pgbackrest/docker-compose.dr.yml \
#                  exec pgbackrest /opt/dr/run-backup.sh full
#
#   full  → --type=full  （周日）
#   diff  → --type=diff  （周一~周六）
#   check → 只做配置校验，不备份
#   info  → 打印仓库内现有备份清单
#
# 退出码即真值：任何非 0 都应触发告警（接入 Prometheus/Alertmanager）。
# 参考调度（cron 语义，实证 external/pgbackrest/doc/xml/user-guide.xml:1515-1520）：
#   #m  h  dom mon dow  command
#   30  06  *   *   0   run-backup.sh full
#   30  06  *   *   1-6 run-backup.sh diff
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MODE="${1:-diff}"

log() { printf '[dr] %s\n' "$*"; }

case "$MODE" in
  full)
    log "全量备份开始 (stanza=$STANZA)"
    # --type=full 由 --type 选项指定（reference.xml:1479 backup 段 / user-guide.xml:1530）
    pgbackrest --stanza="$STANZA" --type=full --log-level-console=info backup
    ;;
  diff)
    log "差异备份开始 (stanza=$STANZA)"
    pgbackrest --stanza="$STANZA" --type=diff --log-level-console=info backup
    ;;
  check)
    log "配置校验 (stanza=$STANZA)"
    # check 命令语义，实证 reference.xml:2331 / user-guide.xml 的 check-configuration 段：
    # 校验 pgbackrest 与 PG 的 archive_command 是否配置正确、所需 WAL 是否已到仓库。
    pgbackrest --stanza="$STANZA" --log-level-console=info check
    ;;
  info)
    pgbackrest --stanza="$STANZA" info
    ;;
  *)
    echo "未知模式 '$MODE'（可用：full | diff | check | info）" >&2
    exit 2
    ;;
esac

log "完成：$MODE"
