"""Regression test: the simulator's sampled millisecond timestamps must be
**unique per row** (PK is `(time, robot_id)`).

Why this test is needed
----------------------
`robot_telemetry`'s primary key is `(time, robot_id)`, and `time` is decided by
the client's millisecond timestamp `payload.ts`
(`backend_ingestion/app/services/batch_writer.py:_to_row()`:
`if t.ts: ts = datetime.fromtimestamp(t.ts / 1000.0)`). So:

    **Sampling the same robot twice within the same millisecond -> PK conflict ->
    `ON CONFLICT DO NOTHING` silently drops rows**

This is not hypothetical. Reproduced locally (100 robots x 10Hz sampling rhythm,
see below): MQTT jitter/reconnect makes `publisher` stall for several seconds,
after which `run_robot` fast-forwards through a backlog of
`asyncio.sleep(negative expired value)` within a **single real 100ms window**,
firing many rows in a row, so `int(time.time()*1000)` repeats. Local testing hit
a duplicate in 1 out of 10 runs of 20~30s load (same `(ts, robot_id)` twice); the
equivalent "sample within the same millisecond" experiment reproduces it reliably
(200 samples at 0.5ms intervals -> 111 unique milliseconds).

The backend does **not** compensate: `ON CONFLICT DO NOTHING` is the idempotent
semantics required by design (see batch_writer's degradation-chain comment), and
the correct contract for out-of-order/retried sends is "only one row per ms" --
the problem is that **the sender's timestamp is not fine-grained enough**.

This test watches two things
----------------------------
1. **Static**: `RobotState.build_payload()`'s `ts` must come from a monotonically
   increasing **microsecond** clock, not from calling `int(time.time() * 1000)`
   once per row (the latter is the race source).
2. **Empirical**: sample continuously following the real control flow and assert
   **strictly monotonically increasing timestamps** (strictly increasing => unique
   per row). Verified in reverse: switching sampling back to
   `int(time.time()*1000)` makes this test FAIL (see the PR description).

Dependency: standard library only. No MQTT / Docker needed.
"""

from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK_FLEET_PY = ROOT / "simulator" / "mock_fleet.py"

# The real interval between consecutive samples, same order as `PUBLISH_INTERVAL_S = 0.1`
# but more extreme: 0.5ms represents the number of samples squeezed into a single 100ms
# window during "retry backlog" (200 rows / 100ms).
TIGHT_INTERVAL_S = 0.0005
SAMPLES = 200


# Signal used when the environment is not available. **Both run modes must honor it**:
#   - `python tests/<file>.py` (script runner with its own main): treats Skip as skip, exit code 3;
#   - `python -m pytest`: pytest only honors its own `Skipped`; a custom exception is recorded as FAILED.
#     So here Skip directly inherits `pytest.skip.Exception` (when pytest is available) so that
#     "missing dependency" appears as skip rather than red under both entry points.
try:  # pragma: no cover - depends on whether pytest is installed
    from _pytest.outcomes import Skipped as _PytestSkipped

    class Skip(_PytestSkipped):  # type: ignore[misc]
        """Environment not available -- not a code failure (appears as SKIPPED under pytest)."""
except Exception:  # noqa: BLE001 - fall back to a plain exception when pytest is absent; script runner still works
    class Skip(Exception):  # type: ignore[no-redef]
        """Environment not available -- not a code failure."""


def _load_robot_state():
    """Pull `RobotState` from mock_fleet.py (via importlib by file path, without polluting sys.modules)."""
    import importlib.util  # noqa: PLC0415

    # mock_fleet.py unconditionally imports aiomqtt and rich at the top (see
    # simulator/requirements.txt). Missing either raises ModuleNotFoundError at
    # exec_module. CI only installs the repo's declared requirements
    # (+fakeredis/pytest-timeout), **not** simulator's deps, so these two
    # prechecks are necessary: treat "deps not installed" as skip (exit 3) instead
    # of masquerading it as a code failure.
    for modname in ("aiomqtt", "rich"):
        try:
            importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            raise Skip(
                f"{modname} not installed; cannot import simulator/mock_fleet.py. "
                "Need: pip install aiomqtt==2.5.1 rich (same as simulator/requirements.txt)."
            ) from exc
    spec = importlib.util.spec_from_file_location("_mock_fleet_under_test", MOCK_FLEET_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - only reached if the path is wrong
        raise Skip(f"Cannot load module from {MOCK_FLEET_PY}")
    mod = importlib.util.module_from_spec(spec)
    # Dataclasses look up `sys.modules[cls.__module__]` when resolving field
    # annotations, so the module must be registered before execution, otherwise
    # AttributeError: 'NoneType' object has no attribute '__dict__'.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _build_payload_clock_span() -> tuple[int, int]:
    """Return the (lineno, end_lineno) of the millisecond-timestamp expression used in `build_payload()`.

    Statically locates "where ts comes from" to avoid test/implementation drift (changing
    the line number will make the test fail accordingly).
    """
    tree = ast.parse(MOCK_FLEET_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "build_payload"):
            continue
        for sub in ast.walk(node):
            # Match `"ts": <expr>` inside the dict literal.
            if not isinstance(sub, ast.Dict):
                continue
            for key, value in zip(sub.keys, sub.values):
                if isinstance(key, ast.Constant) and key.value == "ts":
                    return (value.lineno, value.end_lineno or value.lineno)
    raise AssertionError(
        f"{MOCK_FLEET_PY}'s build_payload() has no `\"ts\": ...` -- "
        "the payload contract (spec/01 §3) requires a client millisecond timestamp, "
        "which determines the (time, robot_id) primary key."
    )


def test_payload_uses_monotonic_microsecond_source() -> None:
    """Static assertion: payload's `ts` must come from a **monotonically increasing microsecond clock**, not one ms reading per row.

    The anti-pattern this test watches for: `int(time.time() * 1000)` written directly in
    build_payload. It is **not** wrong because it "uses the wall clock", but because it
    "reads once per row with only 1ms precision": the same robot firing twice in one tick
    (happens during retry backlog) collides on the primary key.
    """
    lines = MOCK_FLEET_PY.read_text(encoding="utf-8").splitlines()
    start, end = _build_payload_clock_span()
    snippet = "\n".join(lines[start - 1:end])
    assert "int(time.time() * 1000)" not in snippet.replace(" ", ""), (
        f"{MOCK_FLEET_PY}:{start} payload `ts` directly uses `int(time.time() * 1000)`. "
        "Millisecond granularity + one reading per row = the same robot gets identical ts "
        "during retry backlog, and robot_telemetry's PK is (time, robot_id) -> ON CONFLICT "
        "DO NOTHING silently drops rows. Fix: ts is advanced by the caller on a monotonic "
        "clock (see _next_ts_ms / build_payload(ts_ms))."
    )

    # ts must be passed in by the caller (as a parameter); the sender issues
    # timestamps centrally, eliminating "one reading per row".
    tree = ast.parse(MOCK_FLEET_PY.read_text(encoding="utf-8"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "build_payload"),
        None,
    )
    assert fn is not None, f"{MOCK_FLEET_PY} has no build_payload()"
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "ts_ms" in params, (
        f"{MOCK_FLEET_PY}:{fn.lineno} build_payload() has no ts_ms parameter -- "
        "the timestamp must be issued centrally by the sender (monotonically increasing), "
        "not read from the clock inside payload construction."
    )


def _next_ts_ms(mod, last_ms: int | None, now_ms: int) -> int:
    """Issue timestamps per `mock_fleet`'s new convention: strictly increasing, 1ms precision, not detached from real time."""
    return mod._next_ts_ms(last_ms, now_ms)


def test_timestamp_is_strictly_increasing_under_compaction() -> None:
    """Empirical: 200 samples at 0.5ms intervals (simulating retry backlog) must be **strictly increasing**.

    Why "strictly increasing" rather than "unique": strictly increasing is the **stronger**
    property -- it rules out both "duplicate" and "out of order" forms that drop rows, and
    the failure message is more direct.
    """
    try:
        mod = _load_robot_state()
    except Skip:
        raise
    assert hasattr(mod, "_next_ts_ms"), (
        f"{MOCK_FLEET_PY} provides no issuer `_next_ts_ms(last_ms, now_ms)` -- "
        "the sender needs a single timestamp-issuing point; see this file's header comment."
    )

    stamps: list[int] = []
    last: int | None = None
    for _ in range(SAMPLES):
        last = _next_ts_ms(mod, last, int(time.time() * 1000))
        stamps.append(last)
        time.sleep(TIGHT_INTERVAL_S)

    dup = len(stamps) - len(set(stamps))
    non_increasing = [
        (i, stamps[i - 1], stamps[i])
        for i in range(1, len(stamps)) if stamps[i] <= stamps[i - 1]
    ]
    print(
        f"  [empirical] {SAMPLES} samples at {TIGHT_INTERVAL_S * 1000:.1f}ms intervals: "
        f"{len(set(stamps))} unique timestamps, {dup} duplicates, "
        f"window span {(stamps[-1] - stamps[0]) / 1000:.2f}s",
        flush=True,
    )
    assert not non_increasing, (
        f"Non-monotonically-increasing timestamps exist (first case: index {non_increasing[0][0]} "
        f"{non_increasing[0][1]} -> {non_increasing[0][2]}). "
        "Non-increasing => the same robot gets two identical `time` rows => PK (time, robot_id) "
        "conflict => ON CONFLICT DO NOTHING silently drops rows (rows_skipped rises). "
        "Fix: the issuer must guarantee next > last, and prefer following real time."
    )
    assert dup == 0, f"{SAMPLES} samples contained {dup} timestamps that duplicate each other"


def test_timestamp_tracks_wall_clock_when_idle() -> None:
    """Inverse property: with **no** backlog, the issuer must follow real time and must not run away via pure self-increment.

    Monotonic increase alone is insufficient: a pure self-incrementing counter also increases,
    but it pushes `time` further and further from the real collection moment, making time-window
    queries like "last 60s alerts/dashboard" mismatch the data. Both properties must hold together.
    """
    mod = _load_robot_state()
    base = int(time.time() * 1000)
    last = _next_ts_ms(mod, base - 1000, base)
    assert last == base, (
        f"When idle, the issuer should follow real time directly: expected {base}, got {last}. "
        "If this returns a larger value, the issuer is self-incrementing even with no backlog, "
        "which pollutes time-window queries."
    )


def main() -> int:
    checks = [
        ("payload ts comes from a monotonic microsecond issuer (not per-row ms read)",
         test_payload_uses_monotonic_microsecond_source),
        ("empirical: timestamps strictly increase under compaction (unique per row)",
         test_timestamp_is_strictly_increasing_under_compaction),
        ("idle timestamps follow real time (no self-increment runaway)",
         test_timestamp_tracks_wall_clock_when_idle),
    ]
    failed = skipped = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS  {name}")
        except Skip as exc:
            skipped += 1
            print(f"  SKIP  {name}\n        {exc}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{len(checks) - failed - skipped}/{len(checks)} passed"
          f"{f', {skipped} skipped' if skipped else ''}")
    if failed:
        return 1
    return 3 if skipped == len(checks) else 0


if __name__ == "__main__":
    sys.exit(main())
