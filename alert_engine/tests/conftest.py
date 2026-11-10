"""Test harness: lets `app.main` be imported on a machine **without Redis / TimescaleDB**.

`app.db`'s asyncpg / redis only lazily connect on real calls, but at **import time** it still
constructs connection objects. Here we stub out the two connection entry points of `app.db`,
so the object under test is left with only the pure-Python inhibition + dedup logic.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# Make `import app.*` work (tests run from either the repo root or the alert_engine dir)
ALERT_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ALERT_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ALERT_ENGINE_ROOT))

import pytest  # noqa: E402

# ---- Stub out app.db to avoid tests depending on real Redis / PG ----
_fake_db = types.ModuleType("app.db")


async def _get_pool():                                    # pragma: no cover
    raise RuntimeError("test stub: no database")


async def _get_redis():                                   # pragma: no cover
    raise RuntimeError("test stub: no redis")


async def _close_all() -> None:
    return None


_fake_db.get_pool = _get_pool
_fake_db.get_redis = _get_redis
_fake_db.close_all = _close_all
sys.modules["app.db"] = _fake_db


@pytest.fixture()
def engine(monkeypatch):
    """Return the app.main module with save / publish replaced by in-memory recorders.

    - `save` / `should_emit` are swapped for deterministic in-memory implementations,
      so the test asserts the **inhibition layer**'s behavior, not whether Redis is reachable.
    - `online_state` starts empty each time, simulating early-startup state.
    """
    import app.main as m
    import app.store as store

    emitted: list = []

    async def fake_should_emit(alert, window_s):
        return True                                   # dedup layer always passes

    async def fake_save(alert):
        emitted.append(alert)

    monkeypatch.setattr(m, "should_emit", fake_should_emit)
    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(store, "save", fake_save)

    m.online_state.clear()
    m.windows.clear()
    m.last_seen.clear()
    m.active_alerts.clear_all()                       # Order 30: active alert registry

    m.emitted = emitted                               # exposed for assertions
    yield m
    m.online_state.clear()
    m.active_alerts.clear_all()
    if hasattr(m, "emitted"):
        del m.emitted


def run(coro):
    """Run a coroutine synchronously, avoiding pytest-asyncio markers per test."""
    return asyncio.run(coro)
