"""ISSUE #96 — acceptance test for the offline rule switching to read `robot_metadata.is_online`.

Four semantics that must be nailed down (all are "silently mis-reporting when read wrong"):

1. `is_online=True` → no alert; `False` → alert (the criterion comes from the column, not
   in-memory inference).
2. **Single source of truth**: deliberately feed a `last_seen` "just received telemetry 3s ago"
   but `is_online=False`; the rule must still alert — if it falls back to
   `now - last_seen <= offline_after_s` it would miss this one.
3. **Migration fallback**: `is_online=None` (column not yet written / engine can't read the row)
   falls back to the `last_seen` time-delta judgement, and must **not** hard-judge "no value in
   column" as offline (otherwise a one-time burst of 100 critical false alarms at deploy time).
4. **Switch is reversible**: with `offline_use_is_online=False`, always use the time delta,
   reproducing the pre-#96 behavior.
"""

from __future__ import annotations

import time

from app.config import settings
from app.rules.offline import OfflineRule


def _state(is_online, last_seen=None, rid="humanoid-001"):
    return {"robot_id": rid, "is_online": is_online, "last_seen": last_seen}


# ------------------------------------------------ 1. Column is the criterion

def test_online_robot_emits_nothing():
    rule = OfflineRule()
    assert rule.evaluate(_state(True, time.time()), []) is None


def test_offline_robot_emits_critical():
    rule = OfflineRule()
    alert = rule.evaluate(_state(False, time.time() - 42), [])
    assert alert is not None
    assert alert.rule_id == "offline"
    assert alert.severity == 3
    assert alert.detail["age_s"] == 42.0


def test_never_seen_robot_is_online_false_and_no_last_seen():
    """Never reported telemetry: backend sets is_online to false (heartbeat + timeout scan),
    last_seen is None."""
    rule = OfflineRule()
    alert = rule.evaluate(_state(False, None), [])
    assert alert is not None
    assert alert.detail["age_s"] is None
    assert "never reported" in alert.message


# ------------------------------------------------ 2. Single source of truth

def test_judgement_comes_from_column_not_memory():
    """Just received telemetry (in-memory last_seen is 3s ago) but column says offline → still alert.

    This is direct evidence of "single source": if the rule falls back to in-memory inference,
    this would become None and miss the alarm.
    """
    rule = OfflineRule()
    alert = rule.evaluate(_state(False, time.time() - 3), [])
    assert alert is not None, "rule is inferring from memory instead of reading the is_online column"


# ------------------------------------------------ 3. Migration-period fallback

def test_missing_is_online_falls_back_and_does_not_false_alarm():
    """`is_online=None` + in-memory last_seen is now → no alert (must not treat "no column
    value" as offline)."""
    rule = OfflineRule()
    assert rule.evaluate(_state(None, time.time()), []) is None


def test_missing_is_online_falls_back_to_time_diff():
    """`is_online=None` but in-memory already timed out 11s → alert on time delta (fallback
    semantics identical to pre-#96)."""
    rule = OfflineRule()
    alert = rule.evaluate(_state(None, time.time() - 11.0), [])
    assert alert is not None
    assert alert.detail["age_s"] >= 10.0


def test_missing_both_is_unknown_and_falls_back_to_never_seen():
    """`is_online=None` and last_seen=None → the fallback path's "never reported" branch."""
    rule = OfflineRule()
    alert = rule.evaluate(_state(None, None), [])
    assert alert is not None
    assert alert.detail["age_s"] is None


# ------------------------------------------------ 4. Switch is reversible

def test_switch_off_always_uses_time_diff(monkeypatch):
    """Switch off → even if the column says online, still alert on last_seen timeout
    (reproduce the old behavior)."""
    rule = OfflineRule()
    monkeypatch.setattr(rule, "use_is_online", False)
    alert = rule.evaluate(_state(True, time.time() - 30), [])
    assert alert is not None, "switch off did not take the time-delta path"


def test_switch_default_is_on():
    """Default is to read the column, otherwise T2's semantic change has no effect."""
    assert settings.offline_use_is_online is True
    assert OfflineRule().use_is_online is True
