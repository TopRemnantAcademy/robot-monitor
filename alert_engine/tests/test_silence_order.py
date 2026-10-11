"""订单 17 T3 — 静默（silence）与 emit() 三层过滤顺序 的验收测试。

核心断言：**顺序必须是 silence → inhibition → dedup**（派工单 T3 / 冲突提示）。
验证方式：用探针替换三层，记录**实际调用顺序**，并验证
「静默生效时 inhibition 与 dedup 都不会被触发」（验收标准原文）。

不 mock 被测逻辑本身 —— 断言的是 `app.main.emit()` 的真实控制流。
"""

from __future__ import annotations

import pytest

from tests.conftest import run

from app.rules.base import Alert


def _alert(rid: str = "humanoid-001", rule_id: str = "battery_low",
           severity: int = 2) -> Alert:
    return Alert(robot_id=rid, rule_id=rule_id, severity=severity,
                 message=f"{rule_id} on {rid}", detail={})


@pytest.fixture()
def calls(engine, monkeypatch):
    """替换三层为记录器，返回调用顺序列表。

    - is_silenced  → 由测试控制返回值（默认 False）
    - is_inhibited → 由测试控制返回值（默认 False）
    - should_emit  → 由测试控制返回值（默认 True）
    - save         → 记录最终落库的事件（conftest 已替换）
    """
    order: list[str] = []
    state = {"silenced": False, "inhibited": False, "dedup_pass": True}

    async def probe_silence(alert):
        order.append("silence")
        return state["silenced"]

    def probe_inhibition(alert):
        order.append("inhibition")
        return state["inhibited"]

    async def probe_dedup(alert, window_s):
        order.append("dedup")
        return state["dedup_pass"]

    monkeypatch.setattr(engine, "is_silenced", probe_silence)
    monkeypatch.setattr(engine, "is_inhibited", probe_inhibition)
    monkeypatch.setattr(engine, "should_emit", probe_dedup)
    engine._order = order
    engine._state = state
    return engine


def test_order_is_silence_inhibition_dedup(calls):
    """三层都放行时，调用顺序严格是 silence → inhibition → dedup。"""
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition", "dedup"]
    assert len(calls.emitted) == 1


def test_silenced_skips_inhibition_and_dedup(calls):
    """验收原文：静默生效时，inhibition 与 dedup 都不会被触发。"""
    calls._state["silenced"] = True
    run(calls.emit(_alert()))

    assert calls._order == ["silence"], "静默命中后不得再触达 inhibition/dedup"
    assert "inhibition" not in calls._order
    assert "dedup" not in calls._order
    assert calls.emitted == [], "静默期内不得落库"


def test_silenced_alert_logged_at_debug(calls, caplog):
    """静默丢弃必须留 DEBUG 痕迹（排查「为什么没告警」需要）。"""
    calls._state["silenced"] = True
    with caplog.at_level("DEBUG", logger="alert_engine"):
        run(calls.emit(_alert()))
    assert any("silenced" in r.message for r in caplog.records), caplog.text


def test_inhibited_still_runs_after_silence_pass(calls):
    """静默未命中、抑制命中时：只走到 inhibition，不触达 dedup。"""
    calls._state["inhibited"] = True
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition"]
    assert calls.emitted == []


def test_dedup_runs_last(calls):
    """前两层都放行、dedup 拦截时：三层都被调用，但不落库。"""
    calls._state["dedup_pass"] = False
    run(calls.emit(_alert()))
    assert calls._order == ["silence", "inhibition", "dedup"]
    assert calls.emitted == []


# ------------------------------------------------ is_silenced 自身的语义（真 Redis Key）

def test_is_silenced_hits_robot_key(monkeypatch):
    """命中 `alert:silence:{robot_id}` 即静默。"""
    import app.silence as silence

    class FakeRedis:
        def __init__(self, present):
            self._present = present
            self.calls = []

        async def mget(self, *keys):
            self.calls.append(keys)
            return ["1" if k in self._present else None for k in keys]

    fake = FakeRedis({"alert:silence:humanoid-007"})
    monkeypatch.setattr(silence, "get_redis", lambda: _await(fake))
    assert run(silence.is_silenced(_alert("humanoid-007"))) is True
    assert run(silence.is_silenced(_alert("humanoid-008"))) is False
    # 必须 MGET 同时查全局与单机 Key（一次往返）
    assert fake.calls[0] == ("alert:silence:all", "alert:silence:humanoid-007")


def test_is_silenced_global_key(monkeypatch):
    import app.silence as silence

    class FakeRedis:
        async def mget(self, *keys):
            return ["1" if k == "alert:silence:all" else None for k in keys]

    monkeypatch.setattr(silence, "get_redis", lambda: _await(FakeRedis()))
    assert run(silence.is_silenced(_alert("humanoid-099"))) is True


def test_is_silenced_fail_open(monkeypatch, caplog):
    """Redis 不可达 → fail-open（放行告警），绝不因静默层故障压制故障告警。"""
    import app.silence as silence

    class Boom:
        async def mget(self, *keys):
            raise RuntimeError("redis down")

    monkeypatch.setattr(silence, "get_redis", lambda: _await(Boom()))
    with caplog.at_level("ERROR", logger="silence"):
        assert run(silence.is_silenced(_alert())) is False


async def _await(obj):
    return obj
