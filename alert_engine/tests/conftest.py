"""测试基座：让 `app.main` 能在**没有 Redis / TimescaleDB** 的本机被导入。

`app.db` 里的 asyncpg / redis 只在真正调用时才 lazy 建连，但 `import` 期
仍会构造连接对象。这里用 stub 模块替掉 `app.db` 的两个连接入口，
使被测对象只剩纯 Python 的抑制 + 去重逻辑。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

# 让 `import app.*` 生效（测试从仓库根或 alert_engine 目录跑都行）
ALERT_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ALERT_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ALERT_ENGINE_ROOT))

import pytest  # noqa: E402

# ---- stub 掉 app.db，避免测试依赖真实 Redis / PG ----
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
    """返回 app.main 模块，并把落库/发布替换成内存记录器。

    - `save` / `should_emit` 都被换成确定性的内存实现，
      这样测试断言的是**抑制层**的行为，而不是 Redis 是否可达。
    - `online_state` 每次从空字典开始，模拟启动初期状态。
    """
    import app.main as m
    import app.store as store

    emitted: list = []

    async def fake_should_emit(alert, window_s):
        return True                                   # 去重层恒放行

    async def fake_save(alert):
        emitted.append(alert)

    monkeypatch.setattr(m, "should_emit", fake_should_emit)
    monkeypatch.setattr(m, "save", fake_save)
    monkeypatch.setattr(store, "save", fake_save)

    m.online_state.clear()
    m.windows.clear()
    m.last_seen.clear()
    m.active_alerts.clear_all()                       # 订单 30：活跃告警登记簿

    m.emitted = emitted                               # 供断言
    yield m
    m.online_state.clear()
    m.active_alerts.clear_all()
    if hasattr(m, "emitted"):
        del m.emitted


def run(coro):
    """同步跑一个协程，避免为每个用例引入 pytest-asyncio 标记。"""
    return asyncio.run(coro)
