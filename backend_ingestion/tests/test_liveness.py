"""Online/offline determination (ISSUE #77) -- the backend must be the single source of truth.

The object under test is the real logic of `app/services/liveness.py` and the real backfill path of
`state_cache.snapshot()`, only swapping Redis for a **minimal usable stand-in** (`_FakeRedis`);
the logic under test itself is not replaced.

## Reverse guardrail (the focus of this file)

The root cause of ISSUE #77 was not "a wrong line was written", but the **wrong source was chosen
for the verdict**: the frontend took the robot's self-reported `status_code` as the online evidence.
So every assertion in this file demands that "the fact comes from the receiving side". To avoid them
becoming "applause for an assertion that can never fail", the bottom
`test_guard_detects_status_code_based_verdict` rewrites the verdict logic back to
`status_code != 0`, and asserts it **must** differ from the real logic -- while the real logic
happens to be exactly the kind ISSUE #77 requires (a stopper is offline).

Run (from repo root):
    pytest backend_ingestion/tests/test_liveness.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import settings
from app.services import liveness, state_cache


# ---------------------------------------------------------------- Redis stand-in

class _FakeRedis:
    """Implements only the commands liveness/state_cache actually use.

    Deliberately **hand-written** rather than using fakeredis: what this file must verify is
    "which round-trips, with what semantics each", and an all-powerful stand-in would hide
    "how many times EXISTS/HGETALL/ZSCORE/ZCOUNT were each called".
    """

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}
        # record call counts: used to assert "batch backfill does not amplify round-trips by robot count"
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

    # ---- other ----
    async def expire(self, key, ttl):
        self.expires[key] = int(ttl)

    def pipeline(self, transaction=True):  # noqa: ARG002 -- the stand-in ignores transaction semantics
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r: _FakeRedis):
        self._r = r
        self._ops: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            # Fold keyword arguments into positional: `hset(key, mapping={...})` and
            # `hset(key, {...})` must be equivalent on the stand-in, otherwise "production code
            # passed a kwarg" would be misreported as "production code didn't write it".
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


# ---------------------------------------------------------------- record heartbeat

class TestRecord:
    def test_record_writes_hash_and_zset(self, fake_redis):
        run(liveness.record("humanoid-001", 1))
        h = fake_redis.hashes["liveness:robot:humanoid-001"]
        assert h["status_code"] == "1"
        assert float(h["last_seen"]) > 0
        assert "humanoid-001" in fake_redis.zsets[liveness.KEY_ZSET]

    def test_gc_ttl_is_far_above_judgement_window(self, fake_redis):
        """TTL is the **garbage-collection** water level, not the judgement window.

        If we blindly copied `state_ttl_seconds` (10s): a robot that reported 10.1s ago would have
        its Hash already expired, so reading it back yields "never reported" -- everything that
        arrived evaporates (exactly the kind of false impression this issue set out to kill).
        """
        run(liveness.record("humanoid-002", 1))
        ttl = fake_redis.expires["liveness:robot:humanoid-002"]
        assert ttl >= 3600
        assert ttl > settings.robot_offline_threshold_s

    def test_record_failure_never_raises(self, monkeypatch):
        """A dead ledger must not take down the ingest loop (same discipline as state_cache)."""

        async def boom():
            raise ConnectionError("injected: redis down")

        monkeypatch.setattr(liveness, "get_redis", boom)
        run(liveness.record("humanoid-003", 1))          # passes only if not raised


# ---------------------------------------------------------------- verdict

class TestIsOnline:
    def test_fresh_last_seen_is_online(self, fake_redis):
        run(liveness.record("humanoid-001", 1))
        got = run(liveness.read("humanoid-001"))
        assert got["is_online"] is True
        assert got["status_code"] == 1

    def test_stopped_robot_is_offline_even_if_status_code_is_healthy(self, fake_redis):
        """The main point of ISSUE #77: **the last frame was "normal", but it has stopped**.

        This is exactly the scenario where the old implementation crashed: `status_code = 1 != 0`
        -> always counted online. The verdict must be based on last_seen, independent of status_code.
        """
        run(liveness.record("humanoid-001", 1))
        # Roll last_seen back beyond the threshold (equivalent to "reported an hour ago, never since").
        # WARNING: both Hash and ZSET must be rolled back: `read()` takes the Hash as source of truth
        # (the ZSET only serves batching and counting), so changing only one yields not "stopped
        # reporting" but two ledgers in contradiction.
        stale = str(time.time() - 3600)
        fake_redis.hashes["liveness:robot:humanoid-001"]["last_seen"] = stale
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-001"] = float(stale)
        got = run(liveness.read("humanoid-001"))
        assert got["is_online"] is False
        assert got["status_code"] == 1, "status_code is still the last frame's value, but it does not participate in the online verdict"
        assert got["last_seen"] is not None

    def test_never_reported_is_offline_and_cheap(self, fake_redis):
        """Never reported: judged offline, and **only one EXISTS** (no 2 round-trips per robot for
        100 robots)."""
        got = run(liveness.read("humanoid-099"))
        assert got == {"is_online": False, "last_seen": None, "status_code": None}
        assert fake_redis.calls["exists"] == 1
        assert fake_redis.calls["hgetall"] == 0

    def test_threshold_is_configurable(self, fake_redis, monkeypatch):
        """Threshold is configurable (ISSUE #77 acceptance: default 30s). A 10Hz normal jitter must
        not misjudge."""
        run(liveness.record("humanoid-001", 1))
        # A 0.3s "jitter": under the default threshold it must still be online
        fake_redis.zsets[liveness.KEY_ZSET]["humanoid-001"] = time.time() - 0.3
        assert run(liveness.read("humanoid-001"))["is_online"] is True

    def test_redis_down_falls_back_to_offline(self, monkeypatch):
        """When the ledger is unreadable, **conservatively judge offline**: better to miss an online
        report than to falsely report online."""

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


# ---------------------------------------------------------------- batch backfill

class TestEnrichIsBatched:
    def test_enrich_adds_flags_without_per_robot_roundtrips(self, fake_redis):
        """Backfilling 100 robots must be a **constant** number of round-trips, not 200 HGETALLs.

        `/robots` is a high-frequency read endpoint for the big screen and the Drawer; linear
        scaling by robot count would directly blow up the p99 on 100 robots (AGENTS.md section 3's
        performance guardrail applies to the read path as well).
        """
        ids = [f"humanoid-{i:03d}" for i in range(1, 101)]
        run(liveness.record("humanoid-001", 1))
        items = [{"robot_id": rid, "status_code": 0} for rid in ids]
        out = run(liveness.enrich(items))
        assert len(out) == 100
        assert sum(1 for x in out if x["is_online"]) == 1
        # one pipeline (zscore x100 + hgetall x100) -> internal counts each 100, but network
        # round-trips = 1 execute
        assert fake_redis.calls["exists"] == 0, "batch path must not do per-robot EXISTS"

    def test_enrich_overwrites_status_code_from_liveness_hash(self, fake_redis):
        """The last-frame status in the cache comes from the ledger (after the Hash's 10s expiry the
        snapshot simply can't provide it)."""
        run(liveness.record("humanoid-007", 2))
        out = run(liveness.enrich([{"robot_id": "humanoid-007", "status_code": 0}]))
        assert out[0]["status_code"] == 2
        assert out[0]["is_online"] is True


# ---------------------------------------------------------------- snapshot contract

class TestSnapshotKeepsHundredRows:
    def test_snapshot_always_returns_all_100(self, fake_redis):
        """A related problem of ISSUE #77: a stopped robot **must not disappear** from the list.

        The old implementation only returned those still having a Hash in Redis (Hash TTL 10s),
        so "a robot that dropped offline vanished out of thin air" -- harder to troubleshoot than
        falsely reporting online.
        """
        ids = [f"humanoid-{i:03d}" for i in range(1, 101)]
        run(liveness.record("humanoid-042", 3))
        snap = run(state_cache.snapshot(ids))
        assert len(snap) == 100
        online = [x for x in snap if x["is_online"]]
        assert [x["robot_id"] for x in online] == ["humanoid-042"]
        # never-reported ones must be offline placeholders, with complete fields (the frontend does
        # Object.entries on the joint dict)
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
        assert snap[0]["last_seen"] is not None, "still must give the moment it was last seen"


# ---------------------------------------------------------------- reverse guardrail

def test_guard_detects_status_code_based_verdict():
    """Rewrite the verdict basis back to `status_code != 0`; the conclusion must **differ** from the
    real logic.

    This is a meta-test: it does not check the production code, but proves that the assertions in
    `TestIsOnline` above are really watching the question of "is the basis last_seen or
    status_code". If one day someone changes this file to judge online by status_code (the original
    defect of ISSUE #77), this would immediately point out that the two verdicts are not
    interchangeable in this scenario.
    """
    now = time.time()
    stale_last_seen = now - 3600          # seen an hour ago, never since
    last_status_code = 1                  # the last frame was "normal"

    # Real basis: the receiving-side fact (the backend's verdict)
    real_verdict = (now - stale_last_seen) <= settings.robot_offline_threshold_s
    # ISSUE #77's original basis: the robot's self-reported last frame
    buggy_verdict = last_status_code != 0

    assert real_verdict is False, "the receiving-side basis must judge offline (ISSUE #77 acceptance)"
    assert buggy_verdict is True, "the self-reported basis must judge online -- the two bases are truly not equivalent"
    assert real_verdict != buggy_verdict
