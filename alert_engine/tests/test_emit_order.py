"""Order 24 T1 — contract test for the **execution order** of `emit()`'s short-circuit layers.

Background: `emit()` is a short-circuit chain — whichever layer hits first returns, and the
layers after it never run. Getting the order wrong doesn't error; it silently swallows real
alerts (or conversely fails to suppress what should be suppressed) — a classic "you'd never know
without a test" defect. This file pins the order down with spies as assertions.

Authoritative order (Order 17 §T3 / issue #39):
    silence → inhibition → dedup → save/publish

  - silence outermost: when a human explicitly says "shut up", they don't want the system to
    compute an inhibition amount or touch a dedup key over this matter. If silence were placed
    after inhibition, the silence window would still pollute the `alerts_suppressed` counter,
    and after the silence expires the dedup key might already be wrongly occupied.
  - inhibition next: when the root cause (offline) is active, the downstream derived battery/temp
    alerts have no disposition value and must be blocked **before** dedup — otherwise after the
    inhibition window ends, the "fake" key in the dedup window would keep eating the first real
    alert after recovery for 60s.
  - dedup innermost: only alerts that truly need to be sent are worth occupying the Redis dedup
    window.

⚠️ Coordination with Order 17 (issue #39):
  The silence layer is implemented by Order 17; this order (24) only pins it into the order
  contract. Before the silence layer lands, the `silence`-related cases will **skip** and note
  the dependency, rather than pretend to pass — see `_silence_gate()`. They auto-activate once
  Order 17 is merged.
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
# Order 17 will wire the silence pre-check into emit(). Its implementation name has several
# reasonable choices; here we probe by "most likely name" one by one; if none found, skip
# (dependency not landed), never disguise "skip" as "pass".

_SILENCE_CANDIDATE_NAMES = ("is_silenced", "is_silent", "should_silence")


def _silence_gate(mod):
    """Return the callable for the silence judgement entry in `emit()`, or None if not found.

    Two probing strategies; a hit on either means the silence layer is wired:
      1) app.main exposes a module-level judgement function (is_silenced / is_silent / should_silence)
      2) app.silence submodule exists and provides the above function (emit imports from it)
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


# ================================================================ 1. current order
# These two must be green today: inhibition **before** dedup.


def test_inhibition_short_circuits_dedup(engine, monkeypatch):
    """When inhibited, the deduplicator must not be called even once."""
    calls: list = []

    async def spy(alert, window_s):
        calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "should_emit", spy)
    engine.online_state["humanoid-001"] = False           # already judged offline by ticker

    run(engine.emit(_alert()))

    assert calls == [], "on an inhibition hit, the dedup layer must not be touched (order: inhibition → dedup)"


def test_dedup_runs_when_not_inhibited(engine, monkeypatch):
    """When not inhibited, the deduplicator is called **exactly once** and lets the write through."""
    calls: list = []

    async def spy(alert, window_s):
        calls.append((alert.rule_id, window_s))
        return True

    monkeypatch.setattr(engine, "should_emit", spy)
    engine.online_state["humanoid-001"] = True

    run(engine.emit(_alert()))

    assert calls == [("battery_low", 120)], \
        "when not inhibited, dedup must be called once with the rule window (battery_low window 120s)"
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


def test_dedup_denied_means_no_save(engine, monkeypatch):
    """When dedup denies (returns False), nothing persists — dedup is before save."""
    async def deny(alert, window_s):
        return False

    monkeypatch.setattr(engine, "should_emit", deny)
    engine.online_state["humanoid-001"] = True

    run(engine.emit(_alert()))

    assert engine.emitted == [], "after dedup denial, nothing should persist"


# ================================================================ 2. silence order
# The following cases depend on the silence layer from Order 17 (issue #39). Skip until landed.


def test_silence_short_circuits_inhibition_and_dedup(engine, monkeypatch):
    """On a silence hit: **neither** inhibition **nor** dedup may be touched.

    This is the core assertion of Order 17's "silence → inhibition → dedup" order.
    Putting silence after inhibition (or after dedup) would fail this case."""
    gate = _silence_gate(engine)
    if gate is None:
        pytest.skip("silence layer not landed, depends on issue #39 (Order 17 T3)")

    inhibit_calls: list = []
    dedup_calls: list = []

    # inhibition probe: wrap is_inhibited directly
    real_is_inhibited = engine.is_inhibited

    def spy_is_inhibited(alert):
        inhibit_calls.append(alert.rule_id)
        return real_is_inhibited(alert)

    async def spy_should_emit(alert, window_s):
        dedup_calls.append(alert.rule_id)
        return True

    monkeypatch.setattr(engine, "is_inhibited", spy_is_inhibited)
    monkeypatch.setattr(engine, "should_emit", spy_should_emit)

    # Make the silence judgement return True (silence hit)
    if inspect.iscoroutinefunction(gate):
        async def fake_gate(alert):
            return True
        monkeypatch.setattr(engine, gate.__name__, fake_gate)
    else:
        monkeypatch.setattr(engine, gate.__name__, lambda alert: True)

    engine.online_state["humanoid-001"] = False           # even offline shouldn't reach inhibition
    run(engine.emit(_alert()))

    assert engine.emitted == [], "nothing must persist during the silence window"
    assert inhibit_calls == [], "silence must come before inhibition (otherwise the suppression counter is polluted)"
    assert dedup_calls == [], "silence must come before dedup (otherwise the silence window occupies a dedup key)"


def test_silence_lifted_allows_normal_chain(engine, monkeypatch):
    """When silence misses, the chain still runs through inhibition/dedup (silence must not
    cause collateral damage)."""
    gate = _silence_gate(engine)
    if gate is None:
        pytest.skip("silence layer not landed, depends on issue #39 (Order 17 T3)")

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

    assert dedup_calls == ["battery_low"], "when not silenced, the chain should run inhibition → dedup normally"
    assert [a.rule_id for a in engine.emitted] == ["battery_low"]


# ================================================================ 3. order invariants (hold whether or not silence lands)


def test_save_is_last_before_notify(engine, monkeypatch):
    """Persistence must happen **after** dedup passes: dedup denial means save is not called.

    Complements `test_dedup_denied_means_no_save` — here the call order is explicitly recorded."""
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

    assert order == ["dedup", "save"], f"call order should be dedup→save, actual {order}"


def test_inhibited_alert_never_saved_nor_notified(engine, monkeypatch):
    """An inhibited alert: neither persisted nor passed to the notifier (notifier hangs off save)."""
    notified: list = []

    class _Notifier:
        async def send(self, alert):
            notified.append(alert)

    monkeypatch.setattr(engine, "_notifier", _Notifier())
    engine.online_state["humanoid-001"] = False

    run(engine.emit(_alert()))

    assert engine.emitted == []
    assert notified == [], "an inhibited alert must not trigger a notification"
