#!/usr/bin/env bash
# Reproducible build for the iso21423_rs Rust extension (pyo3 + maturin).
#
# Usage:
#   ./build.sh develop   # compile + install into a venv (maturin develop)
#   ./build.sh wheel     # build a release wheel into target/wheels/
#   ./build.sh clean     # remove build artifacts
#
# maturin develop REQUIRES a virtualenv (it refuses to run against a bare
# interpreter). This script activates an existing .venv if one is found by
# walking up from this directory, otherwise it creates one at the repo root.
# Works on both Windows (Git Bash: .venv/Scripts/activate) and Linux/macOS
# (.venv/bin/activate).
#
# The host Rust target must match the active Python architecture:
#   - on win-arm64 use a native ARM64 Python (e.g. Python311-arm64) with the
#     aarch64-pc-windows-msvc Rust target installed via rustup.
#   - on x86_64 Linux/CI the host target is used automatically.
set -euo pipefail
cd "$(dirname "$0")"

# Locate an existing virtualenv by walking up the directory tree.
find_venv() {
  local d
  d="$(pwd)"
  while [ "$d" != "/" ] && [ -n "$d" ]; do
    if [ -f "$d/.venv/bin/activate" ] || [ -f "$d/.venv/Scripts/activate" ]; then
      echo "$d/.venv"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

# Activate a venv unless one is already active.
if [ -z "${VIRTUAL_ENV:-}" ]; then
  VENV="$(find_venv || true)"
  if [ -z "$VENV" ]; then
    VENV="$(cd ../.. && pwd)/.venv"
    echo "No virtualenv found; creating $VENV"
    python -m venv "$VENV"
  fi
  if [ -f "$VENV/bin/activate" ]; then
    # shellcheck disable=SC1091
    . "$VENV/bin/activate"
  elif [ -f "$VENV/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    . "$VENV/Scripts/activate"
  else
    echo "Could not find an activate script in $VENV" >&2
    exit 1
  fi
fi

python -m pip install --upgrade pip maturin >/dev/null

CMD="${1:-develop}"
case "$CMD" in
  develop)
    python -m maturin develop
    ;;
  wheel)
    python -m maturin build --release
    ;;
  clean)
    rm -rf target
    ;;
  *)
    echo "usage: $0 [develop|wheel|clean]" >&2
    exit 1
    ;;
esac
