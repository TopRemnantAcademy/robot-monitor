"""Notifier unit tests — no network, intercepts requests with a fake urlopen.

⚠️ This file was originally a "hand-rolled script": 14 assertions lived inside `run()` and were
counted by `check()`, so pytest collected **0 tests** — it would never run in CI, yet the
filename `test_*.py` gave the false impression of "test coverage". Rewritten as real pytest
cases, with every assertion preserved.
(How it was found: after merging the branch, `pytest alert_engine/tests/ -q` still showed
11 passed — identical to before the new files were added. That unchanged number was the signal.)

Run with:
    cd alert_engine && python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from app.notifiers.webhook import WebhookNotifier, build_notifier
from app.notifiers.slack import SlackNotifier, build_slack_notifier
from app.rules.base import Alert

# ---------------------------------------------------------------- fixtures


def make_alert(sev: int = 3, rule: str = "motor_temp") -> Alert:
    return Alert(robot_id="humanoid-001", rule_id=rule, severity=sev,
                 message="Joint 3 temperature 82.1°C exceeds 75°C",
                 detail={"joint": 3, "temp": 82.1})


class FakeResp:
    """urllib.request.urlopen returns a context manager used as `with ... as resp`,
    so status/read() must hang off the `__enter__` return value, not the fake object itself."""

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


# ------------------------------------------------- 1. payload shape
# DingTalk vs WeCom differ in key fields; getting it wrong gets a 400 from the peer

def test_dingtalk_payload_uses_markdown_text():
    dd = WebhookNotifier("dingtalk", "http://x").build_payload(make_alert())
    assert dd["msgtype"] == "markdown"
    assert "text" in dd["markdown"] and "content" not in dd["markdown"]
    assert dd["markdown"]["title"].startswith("[Critical]")


def test_wecom_payload_uses_markdown_content():
    wc = WebhookNotifier("wecom", "http://x").build_payload(make_alert())
    assert "content" in wc["markdown"] and "text" not in wc["markdown"]


def test_generic_payload_passes_original_fields():
    gen = WebhookNotifier("generic", "http://x").build_payload(make_alert())
    assert gen["robot_id"] == "humanoid-001"
    assert gen["rule_id"] == "motor_temp"
    assert gen["severity"] == 3


# ------------------------------------------------- 2. severity threshold

def test_below_min_severity_is_not_sent_and_issues_no_request():
    n = WebhookNotifier("generic", "http://x", min_severity=3)
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":0}')) as m:
        ok2 = asyncio.run(n.send(make_alert(sev=2)))
        ok3 = asyncio.run(n.send(make_alert(sev=3)))
    assert ok2 is False          # severity<3 not pushed
    assert ok3 is True           # severity==3 pushed
    assert m.call_count == 1     # threshold filter issues no request at all


# ------------------------------------------------- 3. business-code judgement
# HTTP 200 but errcode!=0 must be treated as a failure

def test_errcode_nonzero_is_treated_as_failure():
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":400,"errmsg":"invalid token"}')):
        assert asyncio.run(WebhookNotifier("dingtalk", "http://x").send(make_alert())) is False


def test_errcode_zero_is_treated_as_success():
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"errcode":0,"errmsg":"ok"}')):
        assert asyncio.run(WebhookNotifier("dingtalk", "http://x").send(make_alert())) is True


# ------------------------------------------------- 4. exceptions never escape
# A notification failure must not bring down the alert engine

def test_network_error_is_swallowed_and_returns_false():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
        # If the implementation were changed to raise outward, this case would fail outright
        # (instead of being silently ignored)
        assert asyncio.run(WebhookNotifier("generic", "http://x").send(make_alert())) is False


# ------------------------------------------------- 5. unconfigured → whole chain skipped

def test_blank_url_builds_no_notifier():
    assert build_notifier(SettingsBlank()) is None


def test_configured_url_wires_kind_and_threshold():
    got = build_notifier(SettingsWecom())
    assert got is not None
    assert got.kind == "wecom"
    assert got.min_severity == 2


# ------------------------------------------------- 6. illegal kind errors immediately
# Prevent a typo'd kind from silently doing nothing

def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        WebhookNotifier("dingding", "http://x")


# ------------------------------------------------- 7. Slack notifier (GitHub issue #8)
# Mirrors the webhook contract: never raises outward, severity threshold, classified failure reason.

def test_slack_payload_is_text_envelope():
    p = SlackNotifier("http://x").build_payload(make_alert())
    assert "text" in p
    assert "[Critical]" in p["text"]
    assert "humanoid-001" in p["text"]


def test_slack_below_min_severity_is_not_sent():
    n = SlackNotifier("http://x", min_severity=3)
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"ok":true}')) as m:
        ok2 = asyncio.run(n.send(make_alert(sev=2)))
        ok3 = asyncio.run(n.send(make_alert(sev=3)))
    assert ok2 is False
    assert ok3 is True
    assert m.call_count == 1


def test_slack_success_on_ok_true():
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"ok":true}')):
        assert asyncio.run(SlackNotifier("http://x").send(make_alert())) is True


def test_slack_failure_on_ok_false_envelope():
    # Slack returns 200 with {"ok": false, "error": "invalid_token"} on a bad token -> business_error.
    with mock.patch("urllib.request.urlopen",
                    return_value=FakeResp(b'{"ok":false,"error":"invalid_token"}')):
        n = SlackNotifier("http://x")
        assert asyncio.run(n.send(make_alert())) is False
        assert n.last_reason == "business_error"


def test_slack_network_error_is_swallowed():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("conn refused")):
        assert asyncio.run(SlackNotifier("http://x").send(make_alert())) is False


def test_slack_blank_url_builds_no_notifier():
    class SettingsBlankSlack:
        slack_webhook_url = ""
        alert_webhook_min_severity = 3

    assert build_slack_notifier(SettingsBlankSlack()) is None


def test_slack_configured_url_wires_threshold():
    class SettingsSlack:
        slack_webhook_url = "https://hooks.slack.com/services/XXX"
        alert_webhook_min_severity = 2

    got = build_slack_notifier(SettingsSlack())
    assert got is not None
    assert got.kind == "slack"
    assert got.min_severity == 2
