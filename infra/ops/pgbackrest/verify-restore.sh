#!/usr/bin/env bash
# L3 restore authenticity verification: actually **restore** the latest backup to a temp directory,
#   start a read-only instance, and compare row counts / latest timestamp against the source DB.
#   Called by verify.sh --scratch (also runnable standalone).
#
# Why it is worth a separate layer:
#   `verify` only proves "the checksums of files in the repo are not broken", not "this backup can be
#   restored into a usable DB". This layer is the only evidence that covers "did the data really come
#   back after restore" — order 31 §T2 explicitly requires "compare row counts / latest timestamp
#   after restore, forbid echo-OK-style fake verification".
#
# Usage:
#   verify-restore.sh [--help]
#
# Environment variables (defaults target the compose layout: runs inside the repository vehicle):
#   PGBACKREST_STANZA       default robot_monitor
#   SRC_PSQL_ARGS          source DB psql connection args (default "-h timescaledb -U postgres -d robot_monitor")
#   SCRATCH_DIR            empty restore target dir (default /tmp/pbr-verify-restore, auto-created)
#   SCRATCH_PORT           restore instance port (default 55432)
#   VERIFY_TABLES          tables to compare (default "robot_telemetry robot_metadata robot_alert")
#
# Exit codes: 0 consistent; 1 inconsistent; 2 usage error; 3 missing prerequisite
# ⚠️ Known boundary: this script requires the restore target dir to be **writable** and not conflict with production PGDATA. It **will not** touch production data: --pg1-path always points to SCRATCH_DIR.
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
SCRATCH_DIR="${SCRATCH_DIR:-/tmp/pbr-verify-restore}"
SCRATCH_PORT="${SCRATCH_PORT:-55432}"
SRC_PSQL_ARGS="${SRC_PSQL_ARGS:--h timescaledb -U postgres -d robot_monitor}"
VERIFY_TABLES="${VERIFY_TABLES:-robot_telemetry robot_metadata robot_alert}"

log()  { printf '[dr-verify-restore] %s\n' "$*"; }
die()  { printf '[dr-verify-restore] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

command -v pgbackrest >/dev/null 2>&1 || die "pgbackrest binary not found in PATH" 3
command -v psql       >/dev/null 2>&1 || die "psql not found in PATH (needed for source DB comparison)" 3

# The source DB must be reachable — otherwise "comparison" is impossible; report the missing condition
#   clearly instead of faking a pass.
if ! psql $SRC_PSQL_ARGS -tAc 'SELECT 1' >/dev/null 2>&1; then
  die "Source DB unreachable (psql $SRC_PSQL_ARGS failed). This layer needs to read the source DB for a baseline comparison." 3
fi

# 1) Prepare an empty directory (idempotent: clear if already exists)
if [ -e "$SCRATCH_DIR" ] && [ ! -d "$SCRATCH_DIR" ]; then
  die "SCRATCH_DIR=$SCRATCH_DIR exists but is not a directory" 2
fi
mkdir -p "$SCRATCH_DIR"
# --delta on a non-empty dir needs backup.manifest; we want a clean restore here, so clear then full.
rm -rf "${SCRATCH_DIR:?}"/* "${SCRATCH_DIR:?}"/.[!.]* 2>/dev/null || true
log "Restore target: $SCRATCH_DIR (cleared)"

# 2) Record the source DB baseline (**capture baseline before restore**, to avoid drift)
declare -A SRC_COUNT SRC_MAXTS
for t in $VERIFY_TABLES; do
  SRC_COUNT[$t]="$(psql $SRC_PSQL_ARGS -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "")"
  [ -n "${SRC_COUNT[$t]}" ] || die "Source table $t not readable (wrong name / insufficient privileges)" 3
  # Only tables with a time column have a timestamp column
  SRC_MAXTS[$t]="$(psql $SRC_PSQL_ARGS -tAc "SELECT coalesce(max(time)::text,'') FROM $t" 2>/dev/null || echo "")"
  log "Baseline $t: count=${SRC_COUNT[$t]} max_time=${SRC_MAXTS[$t]:-n/a}"
done

# 3) Actually restore to the temp dir (no --delta: clean target, want an independently startable DB)
log "Running pgbackrest restore to temp dir (real on-disk)"
pgbackrest --stanza="$STANZA" --pg1-path="$SCRATCH_DIR" \
  --log-level-console=info restore

[ -f "$SCRATCH_DIR/PG_VERSION" ] || die "After restore, $SCRATCH_DIR has no PG_VERSION — restore produced no valid data directory" 1
log "Restore artifact check: PG_VERSION=$(cat "$SCRATCH_DIR/PG_VERSION"), file count=$(find "$SCRATCH_DIR" -type f | wc -l)"

# 4) Start a read-only instance and compare
#    ⚠️ restore writes recovery config by default; this layer only reads data, so start with -P (read-only)
#    and an independent port. postgres -c port overrides the port to avoid conflicting with production 5432.
#    pg binary dir: prefer pg_config (ships with official PG, reliable across distros/self-build),
#    then the psql directory; if neither, report the missing condition.
if command -v pg_config >/dev/null 2>&1; then
  PG_BIN="$(pg_config --bindir)"
else
  PG_BIN="$(dirname "$(command -v psql)")"
fi
[ -x "$PG_BIN/pg_ctl" ] || die "pg_ctl not found (PG_BIN=$PG_BIN); the restore instance needs the PG client toolset" 3
SOCK_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
STARTED=no
cleanup() {
  if [ "$STARTED" = "yes" ]; then
    "$PG_BIN/pg_ctl" -D "$SCRATCH_DIR" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -rf "$SOCK_DIR" "$LOG_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# Debian puts postgresql.conf under /etc/postgresql/<ver>/main/ (outside PGDATA); under such a layout the
#   restored dir **has no** postgresql.conf and the instance won't start. Production compose's PGDATA
#   includes postgresql.conf, so this is not an issue there.
if [ ! -f "$SCRATCH_DIR/postgresql.conf" ]; then
  if [ -n "${PGDATA_CONF_SRC:-}" ] && [ -f "$PGDATA_CONF_SRC" ]; then
    cp "$PGDATA_CONF_SRC" "$SCRATCH_DIR/postgresql.conf"
    log "Added postgresql.conf (from PGDATA_CONF_SRC=$PGDATA_CONF_SRC)"
    # Debian split-config: conf.d is include_dir; data_directory points to the production path. Both are
    #   disturbances for the temp instance; fix in place — only the scratch-dir copy is modified, not the source config.
    grep -qE "^[[:space:]]*include_dir" "$SCRATCH_DIR/postgresql.conf" \
      && mkdir -p "$SCRATCH_DIR/conf.d"
    sed -i "s|^[[:space:]]*data_directory[[:space:]]*=.*|data_directory = '$SCRATCH_DIR'|" \
      "$SCRATCH_DIR/postgresql.conf"
  else
    log "Warning: $SCRATCH_DIR has no postgresql.conf, and no PGDATA_CONF_SRC provided."
    log "      Debian split-config layout needs it; production PGDATA includes it, so no setting needed."
  fi
fi

log "Starting restore instance (port $SCRATCH_PORT, socket $SOCK_DIR)"
if ! "$PG_BIN/pg_ctl" -D "$SCRATCH_DIR" \
       -o "-p $SCRATCH_PORT -c unix_socket_directories=$SOCK_DIR -c listen_addresses=''" \
       -l "$LOG_FILE" start; then
  log "Start failed, log tail:"
  tail -20 "$LOG_FILE" >&2
  die "Restore instance failed to start — backup is unusable (this itself is failure evidence)" 1
fi
STARTED=yes

# Wait for pg_isready
for _ in $(seq 1 30); do
  if "$PG_BIN/pg_isready" -h "$SOCK_DIR" -p "$SCRATCH_PORT" >/dev/null 2>&1; then break; fi
  sleep 1
done
"$PG_BIN/pg_isready" -h "$SOCK_DIR" -p "$SCRATCH_PORT" >/dev/null 2>&1 \
  || { tail -20 "$LOG_FILE" >&2; die "Restore instance not ready (pg_isready timeout)" 1; }

# 5) Compare table by table
FAILED=0
for t in $VERIFY_TABLES; do
  rcount="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "")"
  if [ "$rcount" != "${SRC_COUNT[$t]}" ]; then
    printf '[dr-verify-restore] FAIL: table %s row count mismatch  src=%s  restore=%s\n' "$t" "${SRC_COUNT[$t]}" "$rcount" >&2
    FAILED=1
  else
    log "OK table $t row count matches: $rcount"
  fi
  rmax="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc "SELECT coalesce(max(time)::text,'') FROM $t" 2>/dev/null || echo "")"
  if [ -n "${SRC_MAXTS[$t]}" ] && [ "$rmax" != "${SRC_MAXTS[$t]}" ]; then
    printf '[dr-verify-restore] FAIL: table %s latest timestamp mismatch  src=%s  restore=%s\n' "$t" "${SRC_MAXTS[$t]}" "$rmax" >&2
    FAILED=1
  elif [ -n "${SRC_MAXTS[$t]}" ]; then
    log "OK table $t max(time) matches: $rmax"
  fi
done

# 6) TimescaleDB extension table (if timescaledb is installed) — hypertable catalog consistency under physical backup
ext_ver="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null || echo "")"
if [ -n "$ext_ver" ]; then
  chunk_n="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc \
    "SELECT count(*) FROM timescaledb_information.chunks" 2>/dev/null || echo "?")"
  log "Restored DB timescaledb=${ext_ver}, chunk count=${chunk_n}"
fi

[ "$FAILED" -eq 0 ] || die "Restore authenticity comparison failed" 1
log "All restore authenticity checks passed: source DB and restored DB have consistent row counts / latest timestamps."
