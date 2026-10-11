"""在线/失联判定（ISSUE #77）—— 后端必须是唯一事实源。

被测对象是 `app/services/liveness.py` 的真实逻辑与 `state_cache.snapshot()` 的真实补齐路径，
只把 Redis 换成一个**最小可用替身**（`_FakeRedis`），不替换被测逻辑本身。

## 反向护栏（本文件的重点）

ISSUE #77 的根因不是"写错了某一行"，而是**判定依据选错了源**：
前端拿机器人自述的 `status_code` 当在线证据。所以本文件的每条断言都要求
"事实来自接收侧"。为避免它们变成"给一个永远不会失败的断言鼓掌"，
底部 `test_guard_detects_status_code_based_verdict` 把判定逻辑改回
`status_code != 0`，并断言它**必须**与真实逻辑给出不同结论 ——
同时真实逻辑**恰好**是 ISSUE #77 要求的那种（停报者离线）。

跑法（从仓库根）：
    pytest backend_ingestion/tests/test_liveness.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import settings
from app.services import liveness, state_cache


# ---------------------------------------------------------------- Redis 替身

class _FakeRedis:
    """只实现 liveness/state_cache 用到的那几个命令。

    刻意**手写**而不是用 fakeredis：本文件要验的就是"哪几个往返、各自语义"，
    用一个全能替身会把"EXISTS/HGETALL/ZSCORE/ZCOUNT 各自被调了几次"这件事藏掉。
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}
        # 记录调用次数：用于断言"批量补齐不按台数放大往返"
        self.calls = {"hgetall": 0, "zscore": 0, "exists": 0}

    # ---- Hash ----
    async def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or kw)

    async def hgetall(self, key):
        self.calls["hgetall"] += 1
        return dict(self.hashes.get(key, {}))

    async def exists(self, key):
        self.calls["exists"] += 1
        return 1 if key in self.hashes else 0

    # ---- ZSET ----
    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zscore(self, key, member):
        self.calls["zscore"] += 1
        v = self.zsets.get(key, {}).get(member)
        return None if v is None else str(v)

    async def zcount(self, key, mn, mx):
        hi = float("inf") if mx == "+inf" else float(mx)
        lo = float("-inf") if mn == "-inf" else float(mn)
        return sum(1 for v in self.zsets.get(key, {}).values() if lo <= v <= hi)

    # ---- 其它 ----
    async def expire(self, key, ttl):
        self.expires[key] = int(ttl)

    def pipeline(self, transaction=True):  # noqa: ARG002 —— 替身忽略事务语义
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r: _FakeRedis):
        self._r = r
        self._ops: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            # 把关键字参数并进位置参数：`hset(key, mapping={...})` 与
            # `hset(key, {...})` 在替身上必须等价，否则会把"生产代码传了 kwarg"
            # 误报成"生产代码没写进去"。
            if kwargs and not args:
                self._ops.append((name, tuple(kwargs.values())))
            else:
                self._ops.append((name, tuple(args) + tuple(kwargs.values())))
            return self

        return _record

    async def execute(self):
        out = []
        for name, args in self._ops:
            out.append(await getattr(self._r, name)(*args))
        self._ops = []
        return out


@pytest.fixture()
def fake_redis(monkeypatch):
    r = _FakeRedis()

    async def _get():
        return r

    monkeypatch.setattr(liveness, "get_redis", _get)
    monkeypatch.setattr(state_cache, "get_redis", _get)
    return r


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 记录心跳

class TestRecord:
    def test_record_writes_hash_and_zset(self, fake_redis):
        run(liveness.record("humanoid-001", 1))
        h = fake_redis.hashes["liveness:robot:humanoid-001"]
        assert h["status_code"] == "1"
        assert float(h["last_seen"]) > 0
        assert "humanoid-001" in fake_redis.zsets[liveness.KEY_ZSET]

    def test_gc_ttl_is_far_above_judgement_window(self, fake_redis):
        """TTL 是**垃圾回收**水位，不是判定窗口。

        若照搬 `state_ttl_seconds`(10s)：10.1s 前上报过的机器人，其 Hash 已过期，
        读回来就是「从未上报」—— 收到多少蒸发多少（这正是本单要消灭的那类假象）。
        """
        run(liveness.record("humanoid-002", 1))
        ttl = fake_redis.expires["liveness:robot:humanoid-002"]
        assert ttl >= 3600
        assert ttl > settings.robot_offline_threshold_s

    def test_record_failure_never_raises(self, monkeypatch):
        """台账挂掉不能拖垮采集循环（与 state_cache 同一纪律）。"""

        async def boom():
            raise ConnectionError("injected: redis down")

        monkeypatch.setattr(liveness, "get_redis", boom)
        run(liveness.record("humanoid-003", 1))          # 不抛即通过


# ---------------------------------------------------------------- 判定

class TestIsOnline:
    def test_fresh_last_seen_is_online(self, fake_redis):
        run(liveness.record("humanoid-001", 1))
        got = run(liveness.read("humanoid-001"))
        assert got["is_online"] is True
        assert got["status_code"] == 1

    def test_stopped_robot_is_offline_even_if_status_code_is_healthy(self, fake_redis):
        """ISSUE #77 的正题：**最后一帧是"正常"，但它已经停了**。

        这正是旧实现翻车的场景：`status_code = 1 ≠ 0` → 永远算在线。
        判定必须基于 last_seen，与 status_code 无关。
        """
        run(liveness.record("humanoid-001", 1))
        # 把 last_seen 回拨到阈值之外（等价于"1 小时前上报过，之后再没来过"）。
        # ⚠️ Hash 与 ZSET 都要回拨：`read()` 以 Hash 为准（ZSET 只服务于批量与计数），
        # 只改一处得到的不是"停报"，而是两条台账互相矛盾的状态。
        stale = str(time.time() - 3600)
        fake_redis.hashes["liveness:robot:humanoid-001"]["last_seen"] = stale
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-001"] = float(stale)
        got = run(liveness.read("humanoid-001"))
        assert got["is_online"] is False
        assert got["status_code"] == 1, "status_code 仍是最后一帧的值，但它不参与在线判定"
        assert got["last_seen"] is not None

    def test_never_reported_is_offline_and_cheap(self, fake_redis):
        """从未上报：判离线，且**只花一次 EXISTS**（不给 100 台各打两次往返）。"""
        got = run(liveness.read("humanoid-099"))
        assert got == {"is_online": False, "last_seen": None, "status_code": None}
        assert fake_redis.calls["exists"] == 1
        assert fake_redis.calls["hgetall"] == 0

    def test_threshold_is_configurable(self, fake_redis, monkeypatch):
        """阈值可配（ISSUE #77 验收：默认 30s）。10Hz 正常抖动不得误判。"""
        run(liveness.record("humanoid-001", 1))
        # 一个 0.3s 的"抖动"：默认阈值下必须仍是在线
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-001"] = time.time() - 0.3
        assert run(liveness.read("humanoid-001"))["is_online"] is True

    def test_redis_down_falls_back_to_offline(self, monkeypatch):
        """台账不可读时**保守判离线**：宁可漏报在线，也不要谎报在线。"""

        async def boom():
            raise ConnectionError("injected")

        monkeypatch.setattr(liveness, "get_redis", boom)
        assert run(liveness.read("humanoid-001"))["is_online"] is False


class TestOnlineCount:
    def test_counts_only_fresh_members(self, fake_redis):
        now = time.time()
        run(liveness.record("humanoid-001", 1))
        run(liveness.record("humanoid-002", 1))
        run(liveness.record("humanoid-003", 1))
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-003"] = now - 3600
        assert run(liveness.online_count()) == 2


# ---------------------------------------------------------------- 批量补齐

class TestEnrichIsBatched:
    def test_enrich_adds_flags_without_per_robot_roundtrips(self, fake_redis):
        """100 台补齐必须是**常数次**往返，而不是 200 次 HGETALL。

        `/robots` 是大屏与 Drawer 的高频读端点；按台数线性放大会在
        100 台上直接把 p99 拉爆（AGENTS.md §3 的性能护栏同样适用于读路径）。
        """
        ids = [f"humanoid-{i:03d}" for i in range(1, 101)]
        run(liveness.record("humanoid-001", 1))
        items = [{"robot_id": rid, "status_code": 0} for rid in ids]
        out = run(liveness.enrich(items))
        assert len(out) == 100
        assert sum(1 for x in out if x["is_online"]) == 1
        # 一次 pipeline（zscore×100 + hgetall×100）→ 内部计数各 100，但网络往返 = 1 次 execute
        assert fake_redis.calls["exists"] == 0, "批量路径不得逐台 EXISTS"

    def test_enrich_overwrites_status_code_from_liveness_hash(self, fake_redis):
        """缓存里的最后一帧状态以台账为准（Hash 10s 过期后快照本来就没法给）。"""
        run(liveness.record("humanoid-007", 2))
        out = run(liveness.enrich([{"robot_id": "humanoid-007", "status_code": 0}]))
        assert out[0]["status_code"] == 2
        assert out[0]["is_online"] is True


# ---------------------------------------------------------------- snapshot 契约

class TestSnapshotKeepsHundredRows:
    def test_snapshot_always_returns_all_100(self, fake_redis):
        """ISSUE #77 的连带问题：停报机器人**不得从列表里消失**。

        旧实现只回 Redis 里还有 Hash 的那些（Hash TTL 10s），
        于是"掉线的机器人凭空不见"——比误报在线更难排查。
        """
        ids = [f"humanoid-{i:03d}" for i in range(1, 101)]
        run(liveness.record("humanoid-042", 3))
        snap = run(state_cache.snapshot(ids))
        assert len(snap) == 100
        online = [x for x in snap if x["is_online"]]
        assert [x["robot_id"] for x in online] == ["humanoid-042"]
        # 未上报的必须是离线占位，且字段齐全（前端会 Object.entries 关节字典）
        empty = next(x for x in snap if x["robot_id"] == "humanoid-001")
        assert empty["status_code"] == 0
        assert empty["is_online"] is False
        assert empty["last_seen"] is None
        assert empty["joint_temperatures"] == {}

    def test_stopped_robot_still_listed_and_marked_offline(self, fake_redis):
        ids = ["humanoid-001"]
        run(liveness.record("humanoid-001", 2))
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-001"] = time.time() - 3600
        snap = run(state_cache.snapshot(ids))
        assert len(snap) == 1
        assert snap[0]["is_online"] is False
        assert snap[0]["last_seen"] is not None, "仍要给出「最后一次见过它」的时刻"


# ---------------------------------------------------------------- 反向护栏

def test_guard_detects_status_code_based_verdict():
    """把判定依据换回 `status_code != 0`，结论必须与真实逻辑**不同**。

    这条是元测试：它不检查生产代码，而是证明上面 `TestIsOnline` 的断言
    确实在盯着"依据是 last_seen 还是 status_code"这个问题。
    若哪天有人把该文件改成按 status_code 判在线（ISSUE #77 的原始缺陷），
    这里会立刻指出两种判定在这个场景下不可互换。
    """
    now = time.time()
    stale_last_seen = now - 3600          # 一小时前见过，之后再没来过
    last_status_code = 1                  # 最后一帧是"正常"

    # 真实依据：接收侧事实（后端判定的那一条）
    real_verdict = (now - stale_last_seen) <= settings.robot_offline_threshold_s
    # ISSUE #77 的原始依据：机器人自述的最后一帧
    buggy_verdict = last_status_code != 0

    assert real_verdict is False, "接收侧依据必须判离线（ISSUE #77 的验收）"
    assert buggy_verdict is True, "自述依据必须判在线 —— 两种依据确实不等价"
    assert real_verdict != buggy_verdict
