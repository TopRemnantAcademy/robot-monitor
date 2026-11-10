#!/usr/bin/env bash
# Reproducible build for the iso21423_rs Rust extension (pyo3 + maturin).
#
# Usage:
#   ./build.sh develop   # compile + install into the active venv (maturin develop)
#   ./build.sh wheel     # build a release wheel into target/wheels/
#   ./build.sh clean     # remove build artifacts
#
# The host Rust target must match the active Python architecture:
#   - on win-arm64 use a native ARM64 Python (e.g. Python311-arm64) with the
#     aarch64-pc-windows-msvc Rust target installed via rustup.
#   - on x86_64 Linux/CI the host target is used automatically.
set -euo pipefail
cd "$(dirname "$0")"

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
