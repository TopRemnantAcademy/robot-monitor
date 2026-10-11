"""G-6：`robot_metadata.last_seen / is_online` **真的写入**（ISSUE #61）。

为什么单独立一组用例（而不是顺手写在 test_emit_order.py 里）：

这张表的写路径是**唯一**的 —— 1000 msg/s 的遥测 ingest 侧刻意不写
（`backend_ingestion/app/services/liveness.py:23` 的理由：逐条 UPDATE 就是
1000 事务/秒，不可接受），改由告警引擎 ticker 在**状态变化时**低频落库，
闸门是 `alert_engine/app/main.py` ticker 里的 `online_state.get(rid) is not online`。

这条路径坏掉时**看板不会报错**：它只是继续用后端 identity 视图现算的在线数显示绿色，
没人会注意到 robot_metadata 已经不再更新。所以需要能红的断言。

三条断言，缺一不可：

1. SQL 真的打在 `robot_metadata` 上且 `$1/$2` 参数顺序正确 ——
   只断言"调用了某个函数"等于没测：把表名改成 robot_status 照样全绿；
2. DB 抛异常时必须**吞掉**（`log.exception`），不能顺带崩掉整条 ticker ——
   一次 PG 抖动若带崩 ticker，所有离线判定就停摆了，远比"元数据暂时旧一点"严重；
3. **低频**：ticker 是每秒一轮的紧循环，闸门一旦失效就是 100 次 UPDATE/秒。
   用例把 `tick_interval_s` 打成 0 跑几百轮，断言每个机器人每个迁移恰好 1 次写入。
"""

from __future__ import annotations

import asyncio
import time

from tests.conftest import run

from app.config import settings
from app import store


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *a):
        return False


class _RecConn:
    def __init__(self, sink: list):
        self._sink = sink

    async def execute(self, sql: str, *args):
        self._sink.append((sql, args))


class _RecPool:
    def __init__(self, sink: list):
        self._sink = sink

    def acquire(self):
        return _AsyncCtx(_RecConn(self._sink))


class _BoomPool:
    """`acquire()` 直接抛：模拟连接池耗尽 / PG 网络中断。"""

    def acquire(self):
        raise ConnectionError("injected: pool exhausted")


def _patched_get_pool(pool):
    async def _get_pool():
        return pool

    return _get_pool


# ------------------------------------------------- 1. SQL 落点正确

def test_update_online_writes_the_metadata_row(monkeypatch):
    """必须 UPDATE robot_metadata，且 `is_online=$2` / `WHERE robot_id=$1` 的顺序对。

    参数顺序写反时语句照样执行成功（类型不符才会报错，而这里两列恰好都是布尔/字符串
    之外的组合），沉默地把错误数据写进正确的表 —— 只有断言参数元组才拦得住。
    """
    sink: list = []
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_RecPool(sink)))

    run(store.update_online("humanoid-042", True))

    assert len(sink) == 1, f"期望 1 次 UPDATE，实际 {len(sink)} 次"
    sql, args = sink[0]
    assert "UPDATE robot_metadata" in sql
    assert "SET is_online=$2" in sql
    assert "last_seen=NOW()" in sql
    assert "WHERE robot_id=$1" in sql
    assert args == ("humanoid-042", True), f"参数元组错误：{args}"


def test_update_online_writes_false_on_offline_transition(monkeypatch):
    """下线不能写丢：`$2` 必须是 False，不能被上层默认值悄悄吃掉。"""
    sink: list = []
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_RecPool(sink)))

    run(store.update_online("humanoid-007", False))

    assert sink[0][1] == ("humanoid-007", False)


# ------------------------------------------------- 2. DB 炸了也不带崩 ticker

def test_update_online_swallows_db_errors(monkeypatch):
    """元数据写失败只记日志：它不是告警链路，不该有拖垮整条 ticker 的权力。"""
    monkeypatch.setattr(store, "get_pool", _patched_get_pool(_BoomPool()))

    # 不抛出来即通过（store.py 内 `except Exception: log.exception(...)`）
    run(store.update_online("humanoid-001", True))


# ------------------------------------------------- 3. 闸门：只在状态变化时写

def test_ticker_writes_metadata_once_per_transition(monkeypatch):
    """主断言：ticker 跑几百轮，每台机器人每个状态迁移只应有 1 次写入。

    `tick_interval_s=0` 下 ticker 是紧循环，若 `online_state` 闸门被后续改动摘掉，
    calls 会变成 100×轮数（几千条），这里立即变红。

    刻意让 offline 规则静默（相位 1 把 `last_seen` 打成 now），把观察对象收敛到
    元数据写路径本身；告警的发射语义不归这条用例管（见 test_emit_order.py）。
    """
    import app.main as m

    calls: list = []
    emitted: list = []

    async def fake_update_online(robot_id: str, online: bool) -> None:
        calls.append((robot_id, online))

    async def fake_emit(alert) -> None:
        emitted.append(alert)

    monkeypatch.setattr(m, "update_online", fake_update_online)
    monkeypatch.setattr(m, "emit", fake_emit)
    monkeypatch.setattr(settings, "tick_interval_s", 0)

    ids = list(settings.robot_ids)
    m.online_state.clear()
    m.windows.clear()
    m.last_seen.clear()

    now = time.time()
    for rid in ids:
        m.last_seen[rid] = now                        # 相位 1：全部在线

    async def drive():
        task = asyncio.create_task(m.ticker())
        await asyncio.sleep(0.2)                      # 数百轮：在线态稳定，不应重复写
        phase1 = len(calls)
        # 相位 2：全部超时 → 离线迁移，每机器人应各得第 2 次写入，且仅一次
        for rid in ids:
            m.last_seen[rid] = now - settings.offline_after_s * 2
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # spawn() 用的是 create_task，取消 ticker 后再给它们一点 CPU 时间跑完
        await asyncio.sleep(0.05)
        return phase1

    try:
        phase1 = run(drive())
    finally:
        m.online_state.clear()
        m.windows.clear()
        m.last_seen.clear()

    assert phase1 == len(ids), (
        f"在线相位写入 {phase1} 次，期望每台机器人恰好 1 次（共 {len(ids)} 台）—— "
        "online_state 闸门疑似失效，正在每轮重发 UPDATE"
    )
    assert {rid for rid, _ in calls} == set(ids), "有机器人从未写入 robot_metadata"
    assert all(online is True for _, online in calls[: len(ids)]), "在线相位应写 True"

    # 相位 2：每台机器人各 +1 次（不多不少），取值必须是 False
    assert len(calls) == 2 * len(ids), (
        f"相位 2 后共 {len(calls)} 次写入，期望 {2 * len(ids)} 次 —— 迁移闸门重复触发"
    )
    assert all(online is False for _, online in calls[len(ids):]), "离线相位应写 False"
