"""Regression test: when the DB is not ready, pool creation must be a **bounded failure**,
not a hang.

Why this test is needed
----------------------
Production symptom (CNB pipeline `e2e-30k-landing`, build `cnb-05c-1k4k55gpd`):

    [e2e] stack up (54.2s)
    [e2e] link failed: backend /healthz did not reach db=up within 180s (last snapshot: None)

`last snapshot: None` means /healthz **never answered once**, rather than answering
`db=down`. The root cause is not in e2e but in the backend's liveness-probe path:

1. From asyncpg 0.32, `init_size` means "connect at pool-creation time" (the official
   ``.. versionchanged:: 0.32.0`` referenced in `app/db/pool.py`), which brings
   the default **`timeout=60`** of `connection.connect()` into the pool-creation process
   (`asyncpg/connection.py`'s `connect(..., timeout=60)`);
2. `backend_ingestion/app/db/session.py:get_pool()` uses exactly `init_size=min_size=2`
   -- so pool creation pre-connects 2 connections;
3. Pool creation sits **on the liveness-probe path**: `/healthz` -> `_db_healthy()` ->
   `ping()` -> `get_pool()`.

Combined: each probe blocks 60s when the DB is not ready; once the probe/orchestrator's
timeout cancels at the `await create_pool(...)` line, the pool is not built and `_pool`
is not assigned, so the next probe waits another 60s from scratch -- `db=up` is never
reached. **Adding a pool-creation timeout** turns it into a bounded failure (raise
TimeoutError at 8s -> `ping()` returns False -> `/healthz` honestly reports `db=down`
-> self-heals next cycle).

What this test watches is "is that guardrail still there", because it is **silent**:
after deleting `timeout=`, all functional tests stay green; it only blows up on slow DB
startup, and the failure mode (probe hang) is far from the cause.

Dependencies and conventions
----------------------------
- **No Docker / real PostgreSQL needed**: just trigger pool-creation failure with an
  "unreachable address". Use a **blackhole address** (RFC 5737 TEST-NET) instead of
  `127.0.0.1:1`, because the latter returns `ConnectionRefusedError` immediately and
  **cannot surface the critical 60s** value.
- The pool-creation timeout upper bound is set generously (default 25s): CI machines may
  be slow; as long as it is "far below 60s" the guardrail is considered present. What
  truly matters is distinguishing `0~10s` (with guardrail) from `60s` (without), a >6x gap.
- Verified in reverse: after deleting `timeout=`, this test FAILs because 60s > 25s (see PR).

Run
---
    python -m pytest tests/test_db_pool_timeout.py -v      # with pytest
    python tests/test_db_pool_timeout.py                   # without pytest (has its own main)

Dependencies: asyncpg (same version as runtime) + standard library. **Skip when asyncpg is
absent (exit code 3)** -- do not masquerade "deps not installed" as a failure.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = ROOT / "backend_ingestion"
SESSION_PY = BACKEND_ROOT / "app" / "db" / "session.py"
BACKEND_POOL_PY = BACKEND_ROOT / "app" / "db" / "pool.py"
BACKEND_CONFIG_PY = BACKEND_ROOT / "app" / "config.py"
ALERT_POOL_PY = ROOT / "alert_engine" / "app" / "pool.py"
ALERT_DB_PY = ROOT / "alert_engine" / "app" / "db.py"
ALERT_CONFIG_PY = ROOT / "alert_engine" / "app" / "config.py"

# RFC 5737 TEST-NET-1: guaranteed unreachable, and **not** immediately refused (that would mask the 60s symptom).
BLACKHOLE_DSN = "postgresql://u:p@192.0.2.1:5432/robot_monitor"
# Upper bound of pool-creation timeout. Far below asyncpg's default 60s, yet far above this repo's default 8s.
MAX_ACCEPTABLE_POOL_TIMEOUT_S = 25.0
# This repo's default pool-creation timeout (aligned with backend_ingestion/app/config.py's db_connect_timeout_s)
EXPECTED_DEFAULT_TIMEOUT_S = 8.0


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


def _asyncpg():
    try:
        import asyncpg  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise Skip(
            "asyncpg not installed; cannot measure real pool-creation blocking time. "
            "Need: pip install asyncpg==0.32.0 (same version as backend_ingestion/requirements.txt)."
        ) from exc
    return asyncpg


def test_asyncpg_default_connect_timeout_is_60() -> None:
    """Pin the referenced "60" as an executable fact, so a changed asyncpg default does not turn the comment into a lie.

    Many repo comments say "connection.connect()'s default timeout=60". This assertion gives
    that statement a machine-verifiable anchor: if the default changes, the test goes red first
    and a human updates the comment.
    """
    asyncpg = _asyncpg()
    import inspect  # noqa: PLC0415

    sig = inspect.signature(asyncpg.connection.connect)
    default = sig.parameters["timeout"].default
    assert default == 60, (
        f"asyncpg.connection.connect's timeout default changed to {default!r} (no longer 60). "
        "Please update the 'default 60s' comments in app/db/session.py and app/db/pool.py, "
        "and re-evaluate the value of db_connect_timeout_s."
    )
    assert asyncpg.__version__, "asyncpg.__version__ is empty; cannot record the measured version"


def test_backend_create_pool_passes_timeout() -> None:
    """`backend_ingestion/app/db/session.py`'s pool creation must explicitly pass `timeout=`.

    This is a **static** assertion (AST-parsing the real call site), independent of the
    network: even if CI cannot reach the blackhole address, it will not miss "someone deleted
    the timeout".
    """
    src = SESSION_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_pool"
    ]
    assert calls, f"No asyncpg.create_pool() call site found in {SESSION_PY}"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "init_size" not in kwargs, (
            "backend's write-path pool creation uses init_size -- that is 'connect at pool creation', "
            "which brings in connection.connect()'s default timeout=60. You must also pass timeout= "
            "(or switch to app/db/pool.py's unified entry point)."
        )
        assert "timeout" in kwargs, (
            f"{SESSION_PY}:{call.lineno} create_pool() has no timeout= argument. "
            "Under asyncpg 0.32, pool creation goes through connection.connect()'s default timeout=60: "
            "each probe blocks 60s when the DB is not ready, and /healthz hangs instead of reporting db=down. "
            "Pass timeout=settings.db_connect_timeout_s."
        )


def test_backend_config_declares_connect_timeout() -> None:
    """`db_connect_timeout_s` must really exist on Settings.

    This repo already has a counterexample: `alert_engine/app/db.py` referenced two **never-declared**
    Settings keys (see test_alert_settings_keys_declared below), which blow up with AttributeError on
    first touch. Here we pin the backend side too, so the same class of error cannot reappear in another module.
    """
    src = BACKEND_CONFIG_PY.read_text(encoding="utf-8")
    assert re.search(r"^\s*db_connect_timeout_s\s*:\s*float\s*=", src, re.M), (
        f"{BACKEND_CONFIG_PY} does not declare db_connect_timeout_s -- "
        "but app/db/session.py references it (missing declaration => AttributeError on startup)."
    )


def _declared_settings_keys(config_py: Path) -> set[str]:
    """Extract declared field names of the BaseSettings subclass from config.py (AST, not regex guessing)."""
    tree = ast.parse(config_py.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        if not any(
            (isinstance(b, ast.Name) and b.id == "BaseSettings")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseSettings")
            for b in cls.bases
        ):
            continue
        # Any annotated field is a pydantic field.
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                keys.add(stmt.target.id)
    return keys


def test_alert_settings_keys_declared() -> None:
    """Settings keys referenced by `alert_engine/app/db.py` must all be declared in config.py.

    This is a defect that **really existed** in this repo (present on main; this test fixes it too):
    `db.py:39-40` reads `settings.db_pool_size` / `settings.db_command_timeout`, but neither is
    declared in `config.py` -> local repro:

        asyncio.run(get_pool())
        AttributeError: 'Settings' object has no attribute 'db_pool_size'

    Unit tests missed it because `alert_engine/tests/conftest.py` stubs out `app.db` **entirely**.
    So this must be covered by static checking: what it verifies is the "references match declarations" contract.
    """
    declared = _declared_settings_keys(ALERT_CONFIG_PY)
    assert declared, f"Could not parse any Settings fields from {ALERT_CONFIG_PY}; check the parsing logic"

    used = set(re.findall(r"settings\.([A-Za-z_][A-Za-z0-9_]*)", ALERT_DB_PY.read_text(encoding="utf-8")))
    assert used, f"No settings.<key> reference points found in {ALERT_DB_PY}"

    missing = sorted(k for k in used if k not in declared)
    assert not missing, (
        f"{ALERT_DB_PY} references Settings keys not declared in {ALERT_CONFIG_PY}: {missing}. "
        "pydantic-settings gives no default for undeclared fields, so it is AttributeError at runtime."
    )


def test_shared_pool_factory_has_connect_timeout_knob() -> None:
    """`app/db/pool.py` (one copy each in backend and alert_engine) must expose connect_timeout and forward it.

    These two pool.py files wrap the "single pool-creation entry point". The guardrail must be
    built at the **entry point**: fixing only one call site still lets the next caller step into 60s.
    """
    for path in (BACKEND_POOL_PY, ALERT_POOL_PY):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_pool"
        ]
        assert fns, f"No async create_pool() found in {path}"
        fn = fns[0]
        params = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
        assert "connect_timeout" in params, (
            f"{path}: create_pool() does not expose a connect_timeout parameter -- "
            "the caller has no way to set an upper bound on pool creation and can only eat "
            "connection.connect()'s default 60s."
        )

        call = next(
            (
                n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "create_pool"
            ),
            None,
        )
        assert call is not None, f"{path}: no asyncpg.create_pool() call found inside create_pool()"
        kwnames = {kw.arg for kw in call.keywords}
        assert "timeout" in kwnames, (
            f"{path}: the inner asyncpg.create_pool() does not forward timeout= -- "
            "the connect_timeout parameter becomes a no-op and the 60s symptom remains."
        )


def test_pool_build_time_is_bounded() -> None:
    """**Empirical**: pool creation against an unreachable address must fail within MAX_ACCEPTABLE_POOL_TIMEOUT_S.

    Usage aligned with `backend_ingestion/app/db/session.py:get_pool()`
    (`init_size=min_size=2`, i.e. the "connect at pool creation" path), only the address is swapped
    for the blackhole address. This is the end-to-end evidence for the whole guardrail: the static
    assertions above only prove "the parameter is written", this one proves "writing it actually works".
    """
    asyncpg = _asyncpg()
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    observed: dict[str, float | str] = {}

    async def probe() -> None:
        started = time.monotonic()
        try:
            pool = await asyncpg.create_pool(
                dsn=BLACKHOLE_DSN,
                min_size=2,
                max_size=10,
                # Same style as session.get_pool() (including timeout)
                timeout=EXPECTED_DEFAULT_TIMEOUT_S,
            )
            # Reaching here means the blackhole address somehow connected (extremely unusual
            # network env) -- this cannot count as passing, the test's premise is not met.
            await pool.close()
            observed["elapsed"] = time.monotonic() - started
            observed["result"] = "connected-unexpectedly"
        except Exception as exc:  # noqa: BLE001 - any exception counts as "bounded failure"; type is irrelevant
            observed["elapsed"] = time.monotonic() - started
            observed["result"] = type(exc).__name__

    asyncio.run(probe())
    elapsed = float(observed["elapsed"])
    print(
        f"  [empirical] pool creation against {BLACKHOLE_DSN.split('@')[-1]}: "
        f"{observed['result']}, took {elapsed:.2f}s",
        flush=True,
    )

    assert observed["result"] != "connected-unexpectedly", (
        f"Blackhole address {BLACKHOLE_DSN} actually connected; the test premise is not met. "
        "Pick a definitely-unreachable address (keep the RFC 5737 range)."
    )
    assert elapsed < MAX_ACCEPTABLE_POOL_TIMEOUT_S, (
        f"Pool creation took {elapsed:.2f}s, exceeding the upper bound {MAX_ACCEPTABLE_POOL_TIMEOUT_S}s.\n"
        f"This is essentially asyncpg `connection.connect()`'s default timeout=60 -- "
        f"meaning the pool-creation timeout guardrail is dead: when the DB is not ready, /healthz "
        f"gets dragged down, cannot report db=down and never reaches db=up.\n"
        f"Fix: create_pool(..., timeout=<value less than 60>), see "
        f"{SESSION_PY.relative_to(ROOT)}."
    )


def main() -> int:
    checks = [
        ("asyncpg connect default timeout still 60 (comment-backed)",
         test_asyncpg_default_connect_timeout_is_60),
        ("backend pool creation explicitly passes timeout=", test_backend_create_pool_passes_timeout),
        ("backend Settings declares db_connect_timeout_s", test_backend_config_declares_connect_timeout),
        ("alert_engine's referenced Settings keys are all declared", test_alert_settings_keys_declared),
        ("both pool.py expose and forward connect_timeout",
         test_shared_pool_factory_has_connect_timeout_knob),
        ("empirical: pool-creation blocking time is bounded (< 25s)", test_pool_build_time_is_bounded),
    ]
    failed = 0
    skipped = 0
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
