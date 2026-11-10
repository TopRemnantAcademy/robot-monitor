#!/usr/bin/env bash
# pgBackRest backup authenticity verification (one of the three T2 entrypoints, **real assertions, not echo OK**)
#
# Three layers of verification, each judged by evidence:
#   L1 Repository consistency: `pgbackrest verify --verbose` — per-file checksum comparison.
#         ⚠️ Field trap: when verify finds an invalid checksum, the **exit code is still 0**,
#            so this layer parses the output text (status: ok / status: valid / no invalid count),
#            not just the exit code — judging only the exit code would give a false green on a corrupted repo.
#   L2 Backup freshness: parse `info --output=json`, assert
#         - status.code == 0 (stanza healthy)
#         - at least one **full** backup exists (the retention baseline)
#         - latest backup error == false, and timestamp.stop within the tolerance window
#         - WAL archive range is non-empty (archive.min/max both present)
#   L3 Restore authenticity (optional, strongest evidence): actually **restore** the latest backup to a
#         temp directory, start a read-only instance, and compare row counts / latest timestamp against
#         the source DB. Requires the --scratch argument; when the environment can't support it, print a
#         clear skip reason and exit 0.
#
# Usage:
#   verify.sh [--max-age-seconds N] [--scratch] [--help]
#
# Exit codes:
#   0  verification passed (or L3 skipped due to missing environment — will state what is missing)
#   1  verification failed (a real assertion did not hold)
#   2  usage error
#   3  missing prerequisite (no pgbackrest / no jq / stanza unavailable)
#
# `|| true` is forbidden: any unmet assertion exits non-zero, silent failure is forbidden (order 31 §forbidden-item 2).
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
MAX_AGE="${PGBACKREST_MAX_BACKUP_AGE_SECONDS:-86400}"   # default: a successful backup must exist within 24h
SCRATCH="no"

log()  { printf '[dr-verify] %s\n' "$*"; }
die()  { printf '[dr-verify] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }
fail() { printf '[dr-verify] FAIL: %s\n' "$*" >&2; FAILED=1; }
FAILED=0

usage() {
  cat <<'USAGE'
pgBackRest backup authenticity verification (real assertions)

Usage:
  verify.sh [--max-age-seconds N] [--scratch] [--help]

Options:
  --max-age-seconds N   fail if latest successful backup is older than N seconds (default 86400 = 24h)
  --scratch            add L3 "restore authenticity": actually restore to a temp dir and compare row counts/timestamps
  --help               this help

Exit codes: 0 pass; 1 assertion failed; 2 usage error; 3 missing prerequisite
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --max-age-seconds) MAX_AGE="${2:-}"; shift 2 ;;
    --scratch)         SCRATCH="yes"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) printf '[dr-verify] ERROR: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MAX_AGE" in
  ''|*[!0-9]*) die "--max-age-seconds must be a positive integer, received '$MAX_AGE'" 2 ;;
esac

# ── Prerequisites ────────────────────────────────────────────────────────────
command -v pgbackrest >/dev/null 2>&1 || die "pgbackrest binary not found in PATH" 3
command -v jq         >/dev/null 2>&1 || die "jq not found in PATH (the JSON tool recommended by the official user-guide.xml 'Using jq' section)" 3
command -v python3    >/dev/null 2>&1 || die "python3 not found in PATH (used for L2 time-window calculation)" 3

# The stanza must exist and info must be available — otherwise it's "missing prerequisite" (3), not
# "assertion failed" (1).
if ! pgbackrest --stanza="$STANZA" --output=json info >/dev/null 2>&1; then
  die "Cannot get info for stanza '$STANZA' (missing prerequisite). Run backup.sh stanza-create first" 3
fi
_hdr_code="$(pgbackrest --stanza="$STANZA" --output=json info 2>/dev/null | jq -r '.[0].status.code // empty')"
if [ "$_hdr_code" = "1" ]; then
  die "stanza '$STANZA' does not exist (status.code=1 missing stanza path). Run backup.sh stanza-create first" 3
fi

INFO_JSON="$(mktemp)"
trap 'rm -f "$INFO_JSON"' EXIT

# ── L1 Repository file-level consistency ─────────────────────────────────────
echo "== L1 Repository consistency (pgbackrest verify: per-file checksum) =="

# ⚠️ Field trap (pgBackRest 2.59.3): when `verify` finds a **checksum mismatch**,
#    the console prints `invalid checksum` and status: invalid, but the **process exit code is still 0**!
#    —— judging only the exit code = false green. So the output must also be captured and parsed
#    (cannot use an echo-OK-style criterion).
VERIFY_OUT="$(mktemp)"
trap 'rm -f "$INFO_JSON" "$VERIFY_OUT"' EXIT
# ⚠️ Must use --verbose: without it, verify prints nothing when **everything is fine**,
#    and only outputs on error — so "no output" could be either success or swallowed, impossible to judge.
set +e
pgbackrest --stanza="$STANZA" --log-level-console=info --output=text --verbose verify > "$VERIFY_OUT" 2>&1
verify_rc=$?
set -e

if [ "$verify_rc" -ne 0 ]; then
  fail "L1: pgbackrest verify exit code $verify_rc (command-level failure)"
  tail -20 "$VERIFY_OUT" >&2
fi

# Assertion 1: the stanza section must report `status: ok` (only printed with --verbose)
if grep -qE "^[[:space:]]*status: ok[[:space:]]*$" "$VERIFY_OUT"; then
  log "L1a OK: verify reports stanza status: ok"
else
  fail "L1a: verify did not report stanza status: ok"
  grep -E "status:" "$VERIFY_OUT" >&2 || true
fi

# Assertion 2: every backup must be `status: valid`
backup_lines="$(grep -cE "^[[:space:]]*backup: .*status: valid" "$VERIFY_OUT" || true)"
if [ "${backup_lines:-0}" -ge 1 ]; then
  log "L1b OK: verify reports per-backup status: valid (${backup_lines} backups)"
else
  fail "L1b: no backup was marked status: valid by verify"
  grep -E "backup: .*status:" "$VERIFY_OUT" >&2 || true
fi

# Assertion 3: all invalid item counts must be 0 (invalid checksum / missing / size invalid / other)
if grep -qE "invalid checksum|status: invalid|missing: [1-9]|size invalid: [1-9]|other: [1-9]" "$VERIFY_OUT"; then
  fail "L1c: verify found invalid files — repo is corrupted, backup not usable for recovery"
  grep -E "invalid checksum|status: invalid|missing:|size invalid:|other:" "$VERIFY_OUT" | sort -u >&2 || true
else
  log "L1c OK: no invalid/missing/checksum/other anomalies"
fi

# ── Get JSON (stable interface) ──
if ! pgbackrest --stanza="$STANZA" --output=json info > "$INFO_JSON" 2>/dev/null; then
  die "Cannot get info JSON for stanza '$STANZA'" 3
fi

# ── L2 Backup freshness and integrity ───────────────────────────────────────
echo
echo "== L2 Backup freshness and integrity (assert info JSON) =="

status_code="$(jq -r '.[0].status.code // empty' "$INFO_JSON")"
if [ "$status_code" = "0" ]; then
  log "L2a OK: stanza status.code=0 ($(jq -r '.[0].status.message' "$INFO_JSON"))"
else
  fail "L2a: stanza status not ok (status.code=${status_code:-empty}, message=$(jq -r '.[0].status.message // "?"' "$INFO_JSON"))"
fi

backup_count="$(jq -r '.[0].backup | length' "$INFO_JSON")"
full_count="$(jq -r '[.[0].backup[] | select(.type=="full")] | length' "$INFO_JSON")"
if [ "${full_count:-0}" -ge 1 ]; then
  log "L2b OK: repo has ${backup_count} backups total, of which ${full_count} full (retention baseline present)"
else
  fail "L2b: repo has no full backup (full=0), no retention baseline"
fi

# Latest backup: error must be false
if [ "${backup_count:-0}" -ge 1 ]; then
  latest_error="$(jq -r '.[0].backup[-1].error' "$INFO_JSON")"
  latest_label="$(jq -r '.[0].backup[-1].label' "$INFO_JSON")"
  latest_stop="$(jq -r '.[0].backup[-1].timestamp.stop' "$INFO_JSON")"
  if [ "$latest_error" = "false" ]; then
    log "L2c OK: latest backup $latest_label has no error (error=false)"
  else
    fail "L2c: latest backup $latest_label marked error=true (backup set incomplete)"
  fi

  # Freshness: now - latest_stop <= MAX_AGE
  age="$(python3 -c "import sys,time; print(int(time.time()) - int(sys.argv[1]))" "$latest_stop")"
  if [ "$age" -le "$MAX_AGE" ]; then
    log "L2d OK: latest successful backup is ${age}s ago ≤ threshold ${MAX_AGE}s (stop=$(python3 -c "import sys,datetime;print(datetime.datetime.fromtimestamp(int(sys.argv[1])).isoformat())" "$latest_stop"))"
  else
    fail "L2d: latest successful backup is ${age}s ago > threshold ${MAX_AGE}s — backup is stale (RPO broken)"
  fi
else
  fail "L2c/L2d: no backup available to judge freshness"
fi

# WAL archive range non-empty (archive.min / archive.max both present)
arch_min="$(jq -r '.[0].archive[-1].min // empty' "$INFO_JSON")"
arch_max="$(jq -r '.[0].archive[-1].max // empty' "$INFO_JSON")"
if [ -n "$arch_min" ] && [ -n "$arch_max" ]; then
  log "L2e OK: WAL archive range non-empty [${arch_min} .. ${arch_max}] (continuous archive stream established)"
else
  fail "L2e: WAL archive range empty (min='${arch_min}' max='${arch_max}') — archive_command not effective"
fi

# ── L3 Restore authenticity (optional, strongest evidence) ──────────────────
echo
if [ "$SCRATCH" != "yes" ]; then
  log "L3 SKIP: --scratch not specified (no on-disk restore by default)."
  log "           What is needed: --scratch + a writable empty directory + enough disk for the latest backup's"
  log "           uncompressed size; this mode actually restores and starts a read-only instance to compare"
  log "           row counts, see docs/DR-RUNBOOK.md §4.6."
else
  echo "== L3 Restore authenticity (real restore to temp dir + row count/timestamp comparison) =="
  # The restore comparison is delegated to a standalone script, keeping this script purely an assertion orchestrator.
  if [ -x "$(dirname "$0")/verify-restore.sh" ]; then
    if "$(dirname "$0")/verify-restore.sh"; then
      log "L3 OK: restored DB and source DB have consistent row counts / latest timestamp"
    else
      fail "L3: restore authenticity comparison failed"
    fi
  else
    fail "L3: --scratch specified, but no executable verify-restore.sh found"
  fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "[dr-verify] all assertions passed (L1 + L2$([ "$SCRATCH" = yes ] && echo ' + L3'))."
  exit 0
fi
echo "[dr-verify] unpassed assertions exist, see the FAIL lines above." >&2
exit 1
