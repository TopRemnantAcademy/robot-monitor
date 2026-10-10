"""通知器单测 —— 不联网，用假 urlopen 拦截请求。

⚠️ 本文件原为「手工脚本」形态：14 条断言写在 `run()` 里靠 `check()` 计数，
pytest 收集到 **0 个用例**，永远不会在 CI 中执行，却因文件名 `test_*.py`
给人"有测试覆盖"的假象。已改写为真正的 pytest 用例，断言逐条保留。
（发现经过：回收分支后 `pytest alert_engine/tests/ -q` 仍是 11 passed，
与新增文件前完全一致 —— 数字没变就是信号。）

跑法：
    cd alert_engine && python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from app.notifiers.webhook import WebhookNotifier, build_notifier
from app.rules.base import Alert

# ---------------------------------------------------------------- 夹具


def make_alert(sev: int = 3, rule: str = "motor_temp") -> Alert:
    return Alert(robot_id="humanoid-001", rule_id=rule, severity=sev,
                 message="关节 3 温度 82.1°C 超过 75°C",
                 detail={"joint": 3, "temp": 82.1})


class FakeResp:
    """urllib.request.urlopen 返回的是 `with ... as resp` 用的上下文管理器，
    故 status/read() 必须挂在 `__enter__` 的返回值上，而不是假对象本身。"""

    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SettingsBlank:
    alert_webhook_url = ""


class SettingsWecom:
    alert_webhook_url = "https://example.com/hook?token=SE"
    alert_webhook_kind = "wecom"
    alert_webhook_min_severity = 2


# ------------------------------------------------- 1. payload 形状
# 钉钉 vs 企微 的关键字段不同，写错会被对方 400


def test_dingtalk_payload_uses_markdown_text():
    dd = WebhookNotifier("dingtalk", "http://x").build_payload(make_alert())
    assert dd["msgtype"] == "markdown"
    assert "text" in dd["markdown"] and "content" not in dd["markdown"]
    assert dd["markdown"]["title"].startswith("[严重]")


def test_wecom_payload_uses_markdown_content():
    wc = WebhookNotifier("wecom", "http://x").build_payload(make_alert())
    assert "content" in wc["markdown"] and "text" not in wc["markdown"]


def test_generic_payload_passes_original_fields():
    gen = WebhookNotifier("generic", "http://x").build_payload(make_alert())
    assert gen["robot_id"] == "humanoid-001"
    assert gen["rule_id"] == "motor_temp"
    assert gen["severity"] == 3


# ------------------------------------------------- 2. severity 门槛


def test_below_min_severity_is_not_sent_and_issues_no_request():
    n = WebhookNotifier("generic", "http://x", min_severity=3)
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":0}')) as m:
        ok2 = asyncio.run(n.send(make_alert(sev=2)))
        ok3 = asyncio.run(n.send(make_alert(sev=3)))
    assert ok2 is False          # severity<3 不推送
    assert ok3 is True           # severity==3 推送
    assert m.call_count == 1     # 门槛过滤时根本不发请求


# ------------------------------------------------- 3. 业务码判定
# HTTP 200 但 errcode!=0 必须视为失败


def test_errcode_nonzero_is_treated_as_failure():
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":400,"errmsg":"invalid token"}')):
        assert asyncio.run(WebhookNotifier("dingtalk", "http://x").send(make_alert())) is False


def test_errcode_zero_is_treated_as_success():
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":0,"errmsg":"ok"}')):
        assert asyncio.run(WebhookNotifier("dingtalk", "http://x").send(make_alert())) is True


# ------------------------------------------------- 4. 异常不外抛
# 通知失败不能拖垮告警引擎


def test_network_error_is_swallowed_and_returns_false():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
        # 若实现改成向外抛，本用例会直接失败（而不是被静默忽略）
        assert asyncio.run(WebhookNotifier("generic", "http://x").send(make_alert())) is False


# ------------------------------------------------- 5. 未配置则整条链路跳过


def test_blank_url_builds_no_notifier():
    assert build_notifier(SettingsBlank()) is None


def test_configured_url_wires_kind_and_threshold():
    got = build_notifier(SettingsWecom())
    assert got is not None
    assert got.kind == "wecom"
    assert got.min_severity == 2


# ------------------------------------------------- 6. 非法 kind 立即报错
# 防拼错后静默不生效


def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        WebhookNotifier("dingding", "http://x")
