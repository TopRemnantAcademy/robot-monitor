"""ISSUE #96 — offline 规则改读 `robot_metadata.is_online` 的验收。

四条必须钉住的语义（都是「读错会静默误报/漏报」的地方）：

1. `is_online=True` → 不报；`False` → 报（判据来自列，不是内存推算）。
2. **判据单源**：故意喂一个「3 秒前刚收到遥测」的 last_seen 但 `is_online=False`，
   规则仍须报警 —— 若规则回退成用 `now - last_seen <= offline_after_s` 自行推算，
   这条会漏报。
3. **迁移期回落**：`is_online=None`（列还没被写入 / 本引擎读不到该行）时，回落到
   `last_seen` 时间差判定，**不得**把「列暂时没有值」硬判成离线（否则部署初期
   一次性误报 100 台 critical）。
4. **开关可回滚**：`offline_use_is_online=False` 时永远走时间差，复现 #96 之前的行为。
"""

from __future__ import annotations

import time

from app.config import settings
from app.rules.offline import OfflineRule


def _state(is_online, last_seen=None, rid="humanoid-001"):
    return {"robot_id": rid, "is_online": is_online, "last_seen": last_seen}


# ------------------------------------------------ 1. 列即判据

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
    """从未上报过遥测：后端把 is_online 置 false（心跳 + 超时扫描），last_seen 为 None。"""
    rule = OfflineRule()
    alert = rule.evaluate(_state(False, None), [])
    assert alert is not None
    assert alert.detail["age_s"] is None
    assert "从未上报" in alert.message


# ------------------------------------------------ 2. 判据单源

def test_judgement_comes_from_column_not_memory():
    """刚收到遥测（内存 last_seen 是 3s 前）但列判离线 → 仍报警。

    这是「单源」的直接证据：若规则回退成内存推算，这条会变成 None 而漏报。
    """
    rule = OfflineRule()
    alert = rule.evaluate(_state(False, time.time() - 3), [])
    assert alert is not None, "规则在用内存推算而不是读 is_online 列"


# ------------------------------------------------ 3. 迁移期回落

def test_missing_is_online_falls_back_and_does_not_false_alarm():
    """`is_online=None` + 内存 last_seen 是 now → 不报（不得把「列无值」当离线）。"""
    rule = OfflineRule()
    assert rule.evaluate(_state(None, time.time()), []) is None


def test_missing_is_online_falls_back_to_time_diff():
    """`is_online=None` 但内存已超时 11s → 按时间差报（回落语义与 #96 前一致）。"""
    rule = OfflineRule()
    alert = rule.evaluate(_state(None, time.time() - 11.0), [])
    assert alert is not None
    assert alert.detail["age_s"] >= 10.0


def test_missing_both_is_unknown_and_falls_back_to_never_seen():
    """`is_online=None` 且 last_seen=None → 回落路径的「从未上报」分支。"""
    rule = OfflineRule()
    alert = rule.evaluate(_state(None, None), [])
    assert alert is not None
    assert alert.detail["age_s"] is None


# ------------------------------------------------ 4. 开关可回滚

def test_switch_off_always_uses_time_diff(monkeypatch):
    """开关关闭 → 即使列说在线，仍按 last_seen 超时报警（复现旧行为）。"""
    rule = OfflineRule()
    monkeypatch.setattr(rule, "use_is_online", False)
    alert = rule.evaluate(_state(True, time.time() - 30), [])
    assert alert is not None, "开关关闭时未走时间差判定"


def test_switch_default_is_on():
    """默认开启读列，否则 T2 的语义变更等于没生效。"""
    assert settings.offline_use_is_online is True
    assert OfflineRule().use_is_online is True
