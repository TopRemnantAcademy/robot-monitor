#!/usr/bin/env bash
# Restore entrypoint: restore the latest backup by default / point-in-time PITR (runs inside the pgbackrest repository/vehicle)
#
# ⚠️ Dangerous operation: restore will **overwrite** the target cluster's data directory. The script has
#   two built-in confirmation levels.
#
# Usage:
#   # 1) Restore the latest backup (disaster recovery: data directory corrupted/lost)
#   restore.sh latest
#
#   # 2) Point-in-time recovery (accidentally dropped table / modified data, go back to just before the failure)
#   #    Timestamp format: '2026-10-11 08:30:00+08' (timezone required, see user-guide.xml:2248)
#   restore.sh pitr '2026-10-11 08:30:00+08'
#
#   # 3) Restore a specific backup set
#   restore.sh set 20261011-063000F
#
# The restore flow follows the official Quick Start "Restore a Backup" and PITR chapters:
#   - user-guide.xml:1615 the cluster must be stopped and the data directory must be cleared
#   - user-guide.xml:2231 PITR uses --type=time --target=...
#   - restore writes the restore_command needed for recovery (reference.xml:2601)
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MODE="${1:-latest}"
TARGET="${2:-}"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"

log() { printf '[dr-restore] %s\n' "$*"; }

# Pre-check: the data directory must exist and be non-empty before "restore" even applies; otherwise it
#   should be handled in the initialization flow.
if [ ! -d "$PGDATA" ]; then
  echo "Error: PGDATA directory does not exist: $PGDATA" >&2
  exit 3
fi

# Second-level confirmation: non-interactive environments (CI/automation) must explicitly set DR_FORCE_RESTORE=yes
if [ "${DR_FORCE_RESTORE:-no}" != "yes" ]; then
  log "This will overwrite existing data under $PGDATA. Set DR_FORCE_RESTORE=yes to continue."
  exit 4
fi

case "$MODE" in
  latest)
    log "Restoring latest backup (stanza=$STANZA)"
    # --set=latest is the default behavior (reference.xml:2601 restore section); written explicitly for auditability
    pgbackrest --stanza="$STANZA" --set=latest --delta restore
    ;;
  set)
    [ -n "$TARGET" ] || { echo "Usage: restore.sh set <backup-set-name>" >&2; exit 2; }
    log "Restoring specified backup set: $TARGET"
    pgbackrest --stanza="$STANZA" --set="$TARGET" --delta restore
    ;;
  pitr)
    [ -n "$TARGET" ] || { echo "Usage: restore.sh pitr '<YYYY-MM-DD HH:MM:SS+TZ>'" >&2; exit 2; }
    log "Point-in-time recovery (PITR) to: $TARGET"
    # --type=time / --target verified reference.xml:2601 restore section allow-list (time item)
    # --target-action=promote: promote to primary after recovery completes (user-guide.xml:2314 usage)
    # --delta: incremental overwrite based on the existing data directory, avoiding a full re-transfer
    #   (reference.xml:2649 force section)
    pgbackrest --stanza="$STANZA" \
      --type=time --target="$TARGET" --target-action=promote \
      --delta restore
    ;;
  *)
    echo "Unknown mode '$MODE' (available: latest | set <name> | pitr '<ts>')" >&2
    exit 2
    ;;
esac

log "Restore complete. Start PostgreSQL and verify data consistency; see docs/DR-RUNBOOK.md §4."
