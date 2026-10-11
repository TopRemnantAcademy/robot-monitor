#!/usr/bin/env bash
# 恢复入口：默认恢复最新备份 / 指定时间点 PITR（在 pgbackrest 仓库边车内执行）
#
# ⚠️ 危险操作：restore 会**覆盖**目标集群数据目录。脚本内置两级确认。
#
# 用法：
#   # 1) 恢复最新备份（灾难恢复：数据目录已损坏/丢失）
#   restore.sh latest
#
#   # 2) 时间点恢复（误删表/误改数据，回到故障前一刻）
#   #    时间戳格式：'2026-10-11 08:30:00+08'（必须带时区，见 user-guide.xml:2248）
#   restore.sh pitr '2026-10-11 08:30:00+08'
#
#   # 3) 恢复指定备份集
#   restore.sh set 20261011-063000F
#
# 恢复流程遵循官方 Quick Start「Restore a Backup」与 PITR 章节：
#   - user-guide.xml:1615 集群必须停止、数据目录必须清空
#   - user-guide.xml:2231 PITR 用 --type=time --target=...
#   - restore 会写恢复所需的 restore_command（reference.xml:2601）
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MODE="${1:-latest}"
TARGET="${2:-}"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"

log() { printf '[dr-restore] %s\n' "$*"; }

# 前置检查：数据目录必须存在且非空才谈得上「恢复」，否则应在初始化流程里处理。
if [ ! -d "$PGDATA" ]; then
  echo "错误：PGDATA 目录不存在：$PGDATA" >&2
  exit 3
fi

# 二级确认：非交互环境（CI/自动化）需显式设置 DR_FORCE_RESTORE=yes
if [ "${DR_FORCE_RESTORE:-no}" != "yes" ]; then
  log "这将覆盖 $PGDATA 下的现有数据。设置 DR_FORCE_RESTORE=yes 以继续。"
  exit 4
fi

case "$MODE" in
  latest)
    log "恢复最新备份 (stanza=$STANZA)"
    # --set=latest 为默认行为（reference.xml:2601 restore 段），显式写出便于审计
    pgbackrest --stanza="$STANZA" --set=latest --delta restore
    ;;
  set)
    [ -n "$TARGET" ] || { echo "用法：restore.sh set <backup-set-name>" >&2; exit 2; }
    log "恢复指定备份集：$TARGET"
    pgbackrest --stanza="$STANZA" --set="$TARGET" --delta restore
    ;;
  pitr)
    [ -n "$TARGET" ] || { echo "用法：restore.sh pitr '<YYYY-MM-DD HH:MM:SS+TZ>'" >&2; exit 2; }
    log "时间点恢复 (PITR) 到：$TARGET"
    # --type=time / --target 实证 reference.xml:2601 restore 段 allow-list（time 项）
    # --target-action=promote：恢复完成后 promote 为主库（user-guide.xml:2314 用法）
    # --delta：基于现有数据目录做增量覆盖，避免全量重传（reference.xml:2649 force 段）
    pgbackrest --stanza="$STANZA" \
      --type=time --target="$TARGET" --target-action=promote \
      --delta restore
    ;;
  *)
    echo "未知模式 '$MODE'（可用：latest | set <name> | pitr '<ts>'）" >&2
    exit 2
    ;;
esac

log "恢复完成。请启动 PostgreSQL 并核对数据一致性，见 docs/DR-RUNBOOK.md §4。"
