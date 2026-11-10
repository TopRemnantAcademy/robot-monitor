#!/usr/bin/env bash
# DR deliverable **static** self-check (no Docker needed) — runs in CI/locally
#
# Scope (narrowed after order 31 to avoid confusing with verify.sh):
#   This script only checks whether the **file shape is correct**: whether YAML parses, whether the
#   config structure is valid, whether secrets were written into the repo, whether script syntax holds.
#   It does **not** judge whether the backup can be restored — that is verify.sh's job (L1/L2/L3 real
#   assertions). The two are complementary: this script guarantees "config won't fail to start due to
#   syntax", verify.sh guarantees "backup is actually usable".
set -euo pipefail
cd "$(dirname "$0")"

fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }

echo "== 1. YAML parseable =="
for f in docker-compose.dr.yml postgres-exporter-queries.yaml; do
  if python3 -c "import yaml,sys; yaml.safe_load(open('$f'))" 2>/dev/null; then
    ok "$f"
  else
    bad "$f YAML parse failed"
  fi
done

echo "== 2. pgbackrest.conf structure (section + key=value) =="
for f in pgbackrest.conf pgbackrest-db.conf; do
  if python3 - "$f" <<'PY'
import sys
for i,line in enumerate(open(sys.argv[1],encoding="utf-8"),1):
    st=line.strip()
    if not st or st.startswith("#"): continue
    if st.startswith("["):
        assert st.endswith("]"), f"line {i} section not closed"
    else:
        assert "=" in st, f"line {i} missing ="
PY
  then ok "$f structure valid"; else bad "$f structure invalid"; fi
done

echo "== 3. No plaintext secrets =="
if grep -RE 'cipher-pass=[A-Za-z0-9+/]{16,}' pgbackrest.conf pgbackrest-db.conf; then
  bad "suspected plaintext cipher-pass detected"
else
  ok "no plaintext cipher-pass (only \${PGBACKREST_CIPHER_PASS} placeholder)"
fi

echo "== 4. Shell script syntax (incl. order 31's T2 three entrypoints) =="
for s in run-backup.sh backup.sh restore.sh verify.sh verify-restore.sh; do
  if [ ! -f "$s" ]; then bad "$s does not exist"; continue; fi
  if bash -n "$s" 2>/dev/null; then ok "$s"; else bad "$s syntax error"; fi
  # Order 31 §forbidden-item 2: do not use `|| true` / `continue-on-error` to cover **main command** failures.
  # The judgment is tightened to "fallback on the same line as the pgbackrest or the script's own
  #   judging command" — examples in comments, best-effort cleanup in trap (`rm || true`), and the
  #   `|| true` needed when `grep -c` finds no match (otherwise set -e would misjudge "zero match" as a
  #   failure) are not violations.
  hits="$(grep -nE '\|\|[[:space:]]*true|continue-on-error' "$s" 2>/dev/null \
          | grep -vE '^[0-9]+:[[:space:]]*#' \
          | grep -E 'pgbackrest|verify\.sh|backup\.sh|restore\.sh' || true)"
  if [ -n "$hits" ]; then
    printf '  \033[31mFAIL\033[0m %s added a failure fallback for the main command:\n%s\n' "$s" "$hits"
    fail=$((fail+1))
  fi
done

echo "== 5. T2 three entrypoints exist and are executable =="
for s in backup.sh restore.sh verify.sh; do
  if [ -x "$s" ]; then ok "$s executable"; else bad "$s not executable (chmod +x)"; fi
done

echo "== 6. Compose references scripts and files present =="
# Every ./ops/pgbackrest/xxx mounted in the overlay must exist on disk — if one is missing, the container
#   would start but that entry would be an empty file / mount failure (silent failure).
missing=0
for ref in $(grep -oE '\./ops/pgbackrest/[A-Za-z0-9._-]+' docker-compose.dr.yml | sort -u); do
  if [ ! -e "${ref#./ops/pgbackrest/}" ]; then
    bad "compose references a non-existent file: $ref"; missing=1
  fi
done
[ "$missing" -eq 0 ] && ok "all scripts/configs referenced by compose exist"

echo "== 7. shellcheck (if installed) =="
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S warning run-backup.sh backup.sh restore.sh verify.sh verify-restore.sh; then
    ok "shellcheck passed"
  else
    bad "shellcheck reported issues"
  fi
else
  echo "  SKIP shellcheck not installed"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "All static checks passed."
  echo "⚠️  For runtime authenticity, run verify.sh (L1 checksums / L2 freshness / L3 restore comparison)."
else
  echo "$fail items failed."
  exit 1
fi
