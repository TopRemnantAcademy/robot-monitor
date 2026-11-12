#!/bin/bash
# Cross-compile backend_rs release binary for linux/amd64 (Ubuntu/Tencent CVM target).
# Run inside a rust:1-bookworm container on any host arch (arm64 host cross-compiles
# to amd64 natively — no QEMU needed; only the gnu cross-linker is required).
set -euo pipefail

apt-get update -qq
apt-get install -y gcc-x86-64-linux-gnu libssl-dev pkg-config

rustup target add x86_64-unknown-linux-gnu

export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc
cargo build --release --target x86_64-unknown-linux-gnu

echo "=== BUILD ARTIFACT ==="
ls -la target/x86_64-unknown-linux-gnu/release/robot-monitor-backend-rs
file target/x86_64-unknown-linux-gnu/release/robot-monitor-backend-rs 2>/dev/null || true
