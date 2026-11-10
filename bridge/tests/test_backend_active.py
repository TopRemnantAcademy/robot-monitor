"""Regression guard: the compiled Rust backend must be active, not the fallback.

``bridge.iso21423`` and ``bridge.ros2_bridge`` are shims that prefer the compiled
``iso21423_rs`` extension and fall back to pure Python when it is absent. A silent
fallback would hide regressions in the Rust port -- the suite would still "pass"
via the Python implementation, so a broken Rust build could ship green.

These tests fail loudly when the shim resolved to the Python fallback, so any CI
lane that builds the extension cannot regress the port unnoticed.

They are SKIPPED (not failed) when the extension simply is not importable, so a
pure-Python environment (no Rust toolchain) still runs the suite green -- the
caller is expected to build the extension (see bridge/iso21423-rs/build.sh) in the
lanes that are supposed to exercise Rust.
"""

from __future__ import annotations

import importlib.util

import pytest

import bridge.iso21423 as envelope_mod
import bridge.ros2_bridge as ros2_mod

_RUST_AVAILABLE = importlib.util.find_spec("iso21423_rs") is not None


@pytest.mark.skipif(not _RUST_AVAILABLE, reason="iso21423_rs extension not built")
def test_envelope_backend_is_rust() -> None:
    assert envelope_mod._BACKEND == "rust", (
        f"bridge.iso21423 fell back to {envelope_mod._BACKEND!r}; "
        "the Rust extension failed to load (build regression?)"
    )


@pytest.mark.skipif(not _RUST_AVAILABLE, reason="iso21423_rs extension not built")
def test_accumulator_backend_is_rust() -> None:
    assert ros2_mod._ACC_BACKEND == "rust", (
        f"bridge.ros2_bridge fell back to {ros2_mod._ACC_BACKEND!r}; "
        "the Rust accumulator failed to load (build regression?)"
    )
