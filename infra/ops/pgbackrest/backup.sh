#!/usr/bin/env bash
# pgBackRest backup entrypoint (runs on the repository side/vehicle, one of the three T2 entrypoints)
#
# Usage:
#   backup.sh full        full backup (recommended weekly, Sunday)
#   backup.sh diff        differential backup (recommended Mon~Sat)
#   backup.sh incr        incremental backup (optional, block-level incremental)
#   backup.sh check       config/archive-chain verification (run this first after stanza-create)
#   backup.sh info        print existing backup list in the repo (JSON, reused by verify.sh)
#   backup.sh --help      print this help
#
# Contract (order 31 §T2 and §forbidden-item 2):
#   - Idempotent: re-running full/diff will not corrupt the repo; stanza-create is also idempotent.
#   - Any failure exits with **non-zero** and prints a clear reason; **forbid** `|| true` / `continue-on-error`.
#   - Exit codes: 0=success; 2=usage error; 3=missing prerequisite (e.g. stanza not created);
#     others=pgbackrest passed through as-is.
#
# Alert integration (order 31 §T3, reuses #59's existing channel, no separate one built):
#   DB-side `monitor.pgbackrest_info()` (Monitoring example from official user-guide.xml)
#   + postgres_exporter custom query -> `pgbackrest_backup_last_success_timestamp_seconds`
#   -> BackupStale rule in `infra/observability/rules/robot-alerts.yml`.
#   This script itself does **not** push alerts: the exit code is the truth, consumed by the scheduler/rules.
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"

log()  { printf '[dr-backup] %s\n' "$*"; }
die()  { printf '[dr-backup] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'USAGE'
pgBackRest backup entrypoint (runs on the repository side/vehicle, one of the three T2 entrypoints)

Usage:
  backup.sh full        full backup (recommended weekly, Sunday)
  backup.sh diff        differential backup (recommended Mon~Sat)
  backup.sh incr        incremental backup (optional, block-level incremental)
  backup.sh check       config/archive-chain verification (run this first after stanza-create)
  backup.sh info        print existing backup list in the repo (JSON, reused by verify.sh)
  backup.sh stanza-create  create stanza (idempotent; must run first on initial deploy)
  backup.sh --help      print this help

Contract (order 31 §T2 and §forbidden-item 2):
  - Idempotent: re-running full/diff will not corrupt the repo; stanza-create is also idempotent.
  - Any failure exits non-zero and prints a clear reason; forbid `|| true` / `continue-on-error`.
  - Exit codes: 0=success; 2=usage error; 3=missing prerequisite (e.g. stanza not created);
    others=pgbackrest passed through as-is.

Alert integration (order 31 §T3, reuses #59's existing channel, no separate one built):
  DB-side monitor.pgbackrest_info() (Monitoring example from official user-guide.xml)
  + postgres_exporter custom query -> pgbackrest_backup_last_success_timestamp_seconds
  -> BackupStale / BackupFailed rules in infra/observability/rules/robot-alerts.yml.
  This script itself does not push alerts: the exit code is the truth, consumed by scheduler / Prometheus rules.
USAGE
}

MODE="${1:-}"
case "$MODE" in
  ""|-h|--help|help)
    usage
    [ -n "$MODE" ] || exit 2      # No args gives help, but still exits with a usage error to avoid being treated as success
    exit 0
    ;;
esac

# Prerequisite: the pgbackrest binary must be present (DB-side vs repo-side install differs; report clearly if missing)
command -v pgbackrest >/dev/null 2>&1 \
  || die "pgbackrest binary not found in PATH; repo-side image see Dockerfile, DB-side see Dockerfile.timescaledb" 3

# Prerequisite: the stanza must already be created.
#
# ⚠️ Pitfall (verified 2.59.3): `pgbackrest --stanza=<non-existent> info` **still returns 0**,
#   only reporting `status: {"code": N, "message": "..."}` in the JSON (e.g. "missing stanza path").
#   So judging only the exit code would miss it; the JSON status field must be parsed.
if [ "$MODE" != "info" ] && [ "$MODE" != "check" ] && [ "$MODE" != "stanza-create" ]; then
  # jq is the JSON extraction tool recommended by the official user-guide.xml "Using jq" section (both images have it installed).
  status_code="$(pgbackrest --stanza="$STANZA" --output=json info 2>/dev/null | jq -r '.[0].status.code // empty')"
  if [ "$status_code" != "0" ]; then
    die "stanza '$STANZA' unavailable (info status.code=${status_code:-empty}). Run first: backup.sh stanza-create" 3
  fi
fi

case "$MODE" in
  stanza-create)
    log "Creating stanza: $STANZA (idempotent; skipped if already exists)"
    pgbackrest --stanza="$STANZA" --log-level-console=info stanza-create
    ;;
  check)
    log "Verifying config and archive chain: $STANZA"
    # check triggers a pg_switch_wal and confirms the required WAL has reached the repo — semantics verified
    # from external/pgbackrest/doc/xml/reference.xml check command section.
    pgbackrest --stanza="$STANZA" --log-level-console=info check
    ;;
  full|diff|incr)
    log "Starting ${MODE} backup: stanza=$STANZA"
    # --type values full/diff/incr verified: reference.xml backup command section.
    pgbackrest --stanza="$STANZA" --type="$MODE" --log-level-console=info backup
    log "${MODE} backup complete"
    ;;
  info)
    # JSON is a **stable interface** (reference.xml info command section: "kept stable unless a bug is found"),
    # so verify.sh and the alert metric both consume it rather than the version-varying text output.
    pgbackrest --stanza="$STANZA" --output=json info
    ;;
  *)
    printf '[dr-backup] ERROR: unknown mode %s\n' "$MODE" >&2
    printf 'available: full | diff | incr | check | info | stanza-create | --help\n' >&2
    exit 2
    ;;
esac
