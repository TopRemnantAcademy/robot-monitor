#!/usr/bin/env bash
# E2E 30k landing validation (ISSUE #60) -- GitHub Actions entry point.
#
# Word-for-word equivalent to the e2e-30k-landing pipeline in CNB `.cnb.yml`; only the inline script
# originally in the YAML is extracted into this standalone file (logic / SKIP-FAIL banners / exit-code semantics unchanged).
# Design iron rule (from CNB's verified lessons):
#   - exits without a real run must print the fixed [E2E SKIP] string and exit 0, so "green" can no longer fake a real run;
#   - stack-up failure / naming-gate red / 30k-assertion red must all exit non-zero (a gate that can be swallowed is as good as no gate).
# This script is only invoked from the repo root (relative paths tests/e2e/... are based on it).
set -uo pipefail

echo "== e2e 30k landing validation (ISSUE #60) =="

# SKIP banner (any exit without a real run must print this line first, and it must be a grep-able fixed string).
skip_banner() {
  echo "================================================================"
  echo "[E2E SKIPPED -- this build did not actually run the 30k landing]"
  echo "Reason: $1"
  echo "This build's e2e is green but produced NO landing numbers; it cannot count as evidence for ISSUE #60."
  echo "================================================================"
}

# ---- Environment check: no Docker -> explicit skip, never red ----
if ! command -v docker >/dev/null 2>&1; then
  echo "[E2E SKIP] docker command not found in PATH."
  echo "This job needs real Docker (emqx + timescaledb:2.13.1 + redis + backend + mock_fleet),"
  echo "and ISSUE #60 forbids faking real TimescaleDB with a mock stand-in."
  echo "Need: run this job on a build machine/self-hosted runner that provides docker, or run locally:"
  echo "  python -m pytest tests/e2e/test_e2e_30k.py -v -s"
  skip_banner "docker command not found in PATH (runner did not provide Docker-in-Docker)"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "[E2E SKIP] docker daemon unreachable (docker info non-zero). Original output (tail):"
  docker info 2>&1 | tail -5 || true
  skip_banner "docker daemon unreachable"
  exit 0
fi
echo "docker available: $(docker --version)"

# ---- Runtime check: no python at all -> explicit skip, never red ----
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[E2E SKIP] python3/python not found in image PATH (current image: ${CNB_BUILD_IMAGE:-default image})."
  echo "This job needs python to drive tests/e2e/test_e2e_30k.py (standard library only, no pip install needed)."
  echo "Fix: this pipeline must reference the docker.image anchor (python:3.11-slim); do not keep the default image."
  skip_banner "image has no python3/python (docker.image anchor not in effect)"
  exit 0
fi
echo "python available: $($PY --version 2>&1)"

# ---- Order 32 / ISSUE #100: metric-naming antipattern probe ----
# Start the stack first, then assert against the running stack (the naming gate asserts "bytes emitted by the running process").
# Judgment: HTTP unreachable -> [SKIP] and exit 0; old-name residue / new-name missing -> exit code 1 -> this stage turns red.
# The naming-probe rc and the 30k rc are collected independently and merged at the end (neither cuts the other off).
naming_rc=0

echo "== Order 32: metric-naming antipattern probe =="
naming_rc=0
if docker compose -f tests/e2e/docker-compose.e2e.yml up -d --build \
     emqx timescaledb redis backend; then
  i=0
  while [ $i -lt 36 ]; do
    if $PY -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18000/healthz',timeout=3)" >/dev/null 2>&1; then break; fi
    i=$((i + 1)); sleep 5
  done
  if $PY -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18000/metrics',timeout=3)" >/dev/null 2>&1; then
    $PY tests/e2e/test_e2e_30k.py --metric-naming-check || naming_rc=$?
    if [ "$naming_rc" = "3" ]; then
      echo "[E2E SKIP] Order 32 naming probe: script self-judged environment not ready (exit code 3), not a code failure."
      skip_banner "Order 32 naming probe self-judged environment not ready (exit code 3)"
      naming_rc=0
    elif [ "$naming_rc" != "0" ]; then
      echo "[E2E FAIL] Order 32 naming gate FAILED (exit code $naming_rc) -- old-prefix metrics residue or canonical name missing."
    else
      echo "Order 32 naming gate passed: 0 hits on old /metrics names; all robot_backend_* canonical names present."
    fi
  else
    echo "[E2E SKIP] Order 32 naming probe: /metrics unreachable within 180s (stack not ready)"
    skip_banner "Order 32 naming probe: /metrics unreachable within 180s (gate not actually run this round)"
  fi
else
  echo "[E2E FAIL] Order 32 naming probe: stack failed to start."
  echo "(Order 34 tightened the rule: stack-up failure is NO LONGER treated as a silently-skippable environment issue --"
  echo "  this compose stack was already proven startable in a prior real run, so failure means code/config regression.)"
  naming_rc=1
fi
docker compose -f tests/e2e/docker-compose.e2e.yml down -v --remove-orphans || true

# Deps: the e2e script uses only the standard library (subprocess/urllib); no pip install needed.
# Again use `|| rc=$?` to swallow the exit code locally: this is the job's main output, must never be cut off by shell semantics.
echo "== 30k landing main path (this job's hard-threshold output) =="
rc=0
$PY tests/e2e/test_e2e_30k.py || rc=$?
if [ "$rc" = "3" ]; then
  echo "[E2E SKIP] Script self-judged environment not ready (exit code 3), not a code failure."
  skip_banner "test_e2e_30k.py self-judged environment not ready (exit code 3, no Docker / daemon unreachable)"
  rc=0
fi
# Merge judgment: naming-probe red, or 30k red, both turn this stage red (neither is silently passable).
if [ "$rc" = "0" ] && [ "$naming_rc" = "0" ]; then
  echo "e2e stage all passed: 30k landing assertion passed + naming gate in place."
  exit 0
fi
if [ "$naming_rc" != "0" ]; then
  echo "[E2E FAIL] Order 32 naming gate FAILED (exit code $naming_rc) -- old-prefix metrics residue or canonical name missing."
fi
if [ "$rc" != "0" ]; then
  echo "[E2E FAIL] 30k landing assertion FAILED (exit code $rc)."
fi
exit 1
