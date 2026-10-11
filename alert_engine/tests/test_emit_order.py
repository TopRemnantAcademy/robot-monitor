"""订单 24 T1 — `emit()` 短路层的**执行顺序**契约测试。

背景：`emit()` 是一条短路链，任何一层先命中就 return，后面的层根本不会跑。
顺序写错不会报错，只会**静默吞掉真实告警**（或反过来把该压的压不住），
是典型「测试不写就永远不知道」的缺陷。本文件用 spy 把顺序钉成断言。

权威顺序（订单 17 §T3 / issue #39）：
    silence → inhibition → dedup → save/publish

  - silence 最外：人明确「闭嘴」时不希望系统再因为这事算抑制量、动去重 key。
    若把 silence 放在 inhibition 之后，静默期内仍会污染 `alerts_suppressed` 计数，
    且静默到期后 dedup key 可能已被错误占用。
  - inhibition 次之：根因（offline）活跃时，下游派生的 battery/temp 告警无处置价值，
    必须挡在 dedup **之前** —— 否则抑制期一结束，dedup 窗口里那条「假的」key
    会继续把恢复后的第一条真告警吃掉 60s。
  - dedup 最内：只有真的要发的告警才值得占用 Redis 去重窗口。

⚠️ 与订单 17（issue #39）的协同关系：
  silence 层由订单 17 实现，本单（24）只负责把它**钉进顺序契约**。
  在 silence 层落地前，`silence` 相关用例会 **skip** 并注明依赖，
  而不是假装通过 —— 见 `_silence_gate()`。订单 17 合并后自动激活。
"""

from __future__ import annotations

import inspect

import pytest

from tests.conftest import run

from app.rules.base import Alert


def _alert(rid: str = "humanoid-001", rule_id: str = "battery_low",
           severity: int = 2) -> Alert:
    return Alert(robot_id=rid, rule_id=rule_id, severity=severity,
                 message=f"{rule_id} on {rid}", detail={})


# ---------------------------------------------------------------- silence seam
# 订单 17 会把 silence 前置检查接进 emit()。其实现命名有多种合理选择，
# 这里按「最可能的名字」逐一探测；探不到就 skip（依赖未落地），
# 绝不把「跳过」伪装成「通过」。

_SILENCE_CANDIDATE_NAMES = ("is_silenced", "is_silent", "should_silence")


def _silence_gate(mod):
    """返回 `emit()` 里 silence 判定入口的可调用对象，找不到返回 None。

    两种探测策略，任一命中即认为 silence 层已接线：
      1) app.main 暴露模块级判定函数（is_silenced / is_silent / should_silence）
      2) app.silence 子模块存在且提供上述函数（emit 从它 import）
    """
    for name in _SILENCE_CANDIDATE_NAMES:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn

    silence_mod = None
    try:
        import importlib
        silence_mod = importlib.import_module("app.silence")
    except Exception:
        silence_mod = None

    if silence_mod is not None:
        for name in _SILENCE_CANDIDATE_NAMES:
            fn = getattr(silence_mod, name, None)
            if callable(fn):
                return fn
    return None


# ================================================================ 1. 现状顺序
# 这两条今天就必须绿：inhibition **先于** dedup。


def test_inhibition_short_circuits_dedup(engine, monkeypatch):
    """被抑制时，deduplicator 一次都不能被调用。"""
    calls: list = []

    async def spy(alert, window_s):
        calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "should_emit", spy)
    engine.online_state["humanoid-001"] = False           # 已被 ticker 判离线

    run(engine.emit(_alert()))

    assert calls == [], "抑制命中时不应触碰去重层（顺序：inhibition → dedup）"


def test_dedup_runs_when_not_inhibited(engine, monkeypatch):
    """未被抑制时，deduplicator **恰好**被调用一次，并放行落库。"""
    calls: list = []

    async def spy(alert, window_s):
        calls.append((alert.rule_id, window_s))
        return True

    monkeypatch.setattr(engine, "should_emit", spy)
    engine.online_state["humanoid-001"] = True

    run(engine.emit(_alert()))

    assert calls == [("battery_low", 120)], \
        "未抑制时应按规则窗口调用一次去重（battery_low 窗口 120s）"
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_dedup_denied_means_no_save(engine, monkeypatch):
    """去重拒绝（返回 False）时不得落库 —— dedup 在 save 之前。"""
    async def deny(alert, window_s):
        return False

    monkeypatch.setattr(engine, "should_emit", deny)
    engine.online_state["humanoid-001"] = True

    run(engine.emit(_alert()))

    assert engine.emitted == [], "去重拒绝后不应落库"


# ================================================================ 2. silence 顺序
# 以下用例依赖订单 17（issue #39）的 silence 层。未落地时 skip。


def test_silence_short_circuits_inhibition_and_dedup(engine, monkeypatch):
    """静默命中时：inhibition 与 dedup **都不得被触碰**。

    这是订单 17「silence → inhibition → dedup」顺序的核心断言。
    把 silence 放到 inhibition 之后（或 dedup 之后）都会让本用例失败。
    """
    gate = _silence_gate(engine)
    if gate is None:
        pytest.skip("silence 层未落地，依赖 issue #39（订单 17 T3）")

    inhibit_calls: list = []
    dedup_calls: list = []

    # inhibition 探针：直接包住 is_inhibited
    real_is_inhibited = engine.is_inhibited

    def spy_is_inhibited(alert):
        inhibit_calls.append(alert.rule_id)
        return real_is_inhibited(alert)

    async def spy_should_emit(alert, window_s):
        dedup_calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "is_inhibited", spy_is_inhibited)
    monkeypatch.setattr(engine, "should_emit", spy_should_emit)

    # 让 silence 判定返回 True（命中静默）
    if inspect.iscoroutinefunction(gate):
        async def fake_gate(alert):
            return True
        monkeypatch.setattr(engine, gate.__name__, fake_gate)
    else:
        monkeypatch.setattr(engine, gate.__name__, lambda alert: True)

    engine.online_state["humanoid-001"] = False           # 即使离线也不该走到抑制
    run(engine.emit(_alert()))

    assert engine.emitted == [], "静默期内不得落库"
    assert inhibit_calls == [], "silence 必须先于 inhibition（否则抑制计数被污染）"
    assert dedup_calls == [], "silence 必须先于 dedup（否则静默期占用去重 key）"


def test_silence_lifted_allows_normal_chain(engine, monkeypatch):
    """静默未命中时，链路照常走到 inhibition/dedup（silence 不应误伤）。"""
    gate = _silence_gate(engine)
    if gate is None:
        pytest.skip("silence 层未落地，依赖 issue #39（订单 17 T3）")

    dedup_calls: list = []

    async def spy_should_emit(alert, window_s):
        dedup_calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "should_emit", spy_should_emit)

    if inspect.iscoroutinefunction(gate):
        async def fake_gate(alert):
            return False
        monkeypatch.setattr(engine, gate.__name__, fake_gate)
    else:
        monkeypatch.setattr(engine, gate.__name__, lambda alert: False)

    engine.online_state["humanoid-001"] = True
    run(engine.emit(_alert()))

    assert dedup_calls == ["battery_low"], "未静默时应正常走完 inhibition → dedup"
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


# ================================================================ 3. 顺序不变量（无论 silence 是否落地都成立）


def test_save_is_last_before_notify(engine, monkeypatch):
    """落库必须发生在去重放行**之后**：dedup 拒绝则 save 不被调用。

    与 `test_dedup_denied_means_no_save` 互补 —— 这里显式记录调用次序。
    """
    order: list = []

    async def spy_should_emit(alert, window_s):
        order.append("dedup")
        return True

    async def spy_save(alert):
        order.append("save")
        engine.emitted.append(alert)

    monkeypatch.setattr(engine, "should_emit", spy_should_emit)
    monkeypatch.setattr(engine, "save", spy_save)
    engine.online_state["humanoid-001"] = True

    run(engine.emit(_alert()))

    assert order == ["dedup", "save"], f"调用次序应为 dedup→save，实际 {order}"


def test_inhibited_alert_never_saved_nor_notified(engine, monkeypatch):
    """被抑制的告警：既不落库，也不进通知器（通知器挂在 save 之后）。"""
    notified: list = []

    class _Notifier:
        async def send(self, alert):
            notified.append(alert)

    monkeypatch.setattr(engine, "_notifier", _Notifier())
    engine.online_state["humanoid-001"] = False

    run(engine.emit(_alert()))

    assert engine.emitted == []
    assert notified == [], "被抑制的告警不应触发通知"
