#!/usr/bin/env bash
# DR scheduling entrypoint: periodic full + WAL archiving (runs inside the pgbackrest repository/vehicle)
#
# Usage (trigger from host):
#   docker compose -f infra/docker-compose.yml \
#                  -f infra/ops/pgbackrest/docker-compose.dr.yml \
#                  exec pgbackrest /opt/dr/run-backup.sh full
#
#   full  -> --type=full  (Sunday)
#   diff  -> --type=diff  (Mon~Sat)
#   check -> config verification only, no backup
#   info  -> print existing backup list in the repo
#
# Exit code is the truth: any non-zero should trigger an alert (wired to Prometheus/Alertmanager).
# Reference schedule (cron semantics, verified external/pgbackrest/doc/xml/user-guide.xml:1515-1520):
#   #m  h  dom mon dow  command
#   30  06  *   *   0   run-backup.sh full
#   30  06  *   *   1-6 run-backup.sh diff
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MODE="${1:-diff}"

log() { printf '[dr] %s\n' "$*"; }

case "$MODE" in
  full)
    log "Full backup started (stanza=$STANZA)"
    # --type=full specified via the --type option (reference.xml:1479 backup section / user-guide.xml:1530)
    pgbackrest --stanza="$STANZA" --type=full --log-level-console=info backup
    ;;
  diff)
    log "Differential backup started (stanza=$STANZA)"
    pgbackrest --stanza="$STANZA" --type=diff --log-level-console=info backup
    ;;
  check)
    log "Config verification (stanza=$STANZA)"
    # check command semantics, verified reference.xml:2331 / user-guide.xml check-configuration section:
    # verifies whether pgbackrest and PG's archive_command are configured correctly and whether the
    # required WAL has reached the repo.
    pgbackrest --stanza="$STANZA" --log-level-console=info check
    ;;
  info)
    pgbackrest --stanza="$STANZA" info
    ;;
  *)
    echo "Unknown mode '$MODE' (available: full | diff | check | info)" >&2
    exit 2
    ;;
esac

log "Done: $MODE"
