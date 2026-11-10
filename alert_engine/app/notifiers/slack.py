"""Slack notifier: POST a formatted message to a Slack incoming-webhook URL.

Why a separate channel rather than folding it into `webhook.py`:
- Slack's incoming webhook accepts a single, well-defined JSON shape (`{"text": "..."}`, optionally
  Block Kit), and its success envelope is `{"ok": true}` / `{"ok": false, "error": "..."}` -- different
  from DingTalk/WeCom's `errcode`/`errmsg` contract. Mixing the two envelopes into one method would
  mean a `kind == "slack"` branch that diverges on the 200-body check; cleaner to keep it a sibling.
- Reuses the same "never raises outward" + classifiable `last_reason` contract as `WebhookNotifier`
  (R-15 / GAP-3): `send()` returns bool, and on failure it records a reason from the shared
  `NOTIFY_REASONS` set so `app/main.py` can label `robot_alert_notify_failed_total{reason}`.

Failure-reason classification is delegated to `webhook.classify_exception` / `webhook.NotifyError`
(the single source of truth for the closed reason set), so "who classifies" stays in one place.

Disabled by default: when `SLACK_WEBHOOK_URL` is empty, `build_slack_notifier()` returns `None` and
the alert engine's delivery loop simply skips it -- no secrets in the repo, same policy as the webhook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from app.rules.base import Alert
from app.notifiers.webhook import NotifyError, classify_exception

log = logging.getLogger("notifier")

# severity -> display label (keep the same vocabulary on the big screen and in notifications)
_SEV_LABEL = {1: "Info", 2: "Warning", 3: "Critical"}


class SlackNotifier:
    """POST a formatted Alert message to a Slack incoming-webhook URL.

    `min_severity`: alerts below this level are not pushed (default 3, i.e. only critical),
    to avoid "low-battery storm" spam -- same semantics as the webhook notifier.
    """

    #: The failure reason of the most recent `send()` (None on success or when not sent).
    #: Instance attribute (not class attribute) to avoid cross-instance bleed.
    last_reason: str | None = None

    def __init__(self, url: str, min_severity: int = 3, timeout_s: float = 5.0) -> None:
        self.kind = "slack"
        self.url = url
        self.min_severity = min_severity
        self.timeout_s = timeout_s
        self.last_reason = None

    # ------------------------------------------------------------- payload
    def _title(self, alert: Alert) -> str:
        return f"[{_SEV_LABEL.get(alert.severity, "Unknown")}] {alert.rule_id} · {alert.robot_id}"

    def build_payload(self, alert: Alert) -> dict:
        """Slack incoming webhook accepts `{"text": "..."}`; Block Kit is overkill for an alert line."""
        title = self._title(alert)
        body = f"{title}\n\n{alert.message}"
        if alert.detail:
            # detail is optional rule context (e.g. joint id / temperature), compressed to one line
            # for mobile reading.
            body += "\n\n" + json.dumps(alert.detail, ensure_ascii=False, separators=(",", ":"))
        return {"text": body}

    # --------------------------------------------------------------- send
    def _post_blocking(self, payload: dict) -> None:
        """Synchronous POST. On failure always raise `NotifyError` (with reason), **never leak the raw exception**."""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # payload not serializable -- local problem, request not sent; retrying equals replaying the same error.
            raise NotifyError("config", f"payload not serializable: {exc}") from exc

        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", "replace")
                code = resp.status
        except NotifyError:                       # pragma: no cover - defensive
            raise
        except Exception as exc:
            raise NotifyError(classify_exception(exc), f"{type(exc).__name__}: {exc}") from exc

        if code != 200:
            raise NotifyError("http_error", f"slack HTTP {code}: {raw[:200]}")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            # Non-JSON response: treat as delivered (defensive; Slack always returns JSON).
            return
        if body.get("ok") is not True:
            # Slack returns 200 with {"ok": false, "error": "invalid_token"} on a bad/missing token:
            # retry is useless (same token still rejected) -> business_error.
            raise NotifyError("business_error", f"ok=false: {body.get('error')}")

    async def send(self, alert: Alert) -> bool:
        """Return whether it truly delivered. Exceptions are always swallowed and logged -- a notification
        failure must not bring down the alert engine.

        The contract is unchanged (`bool`, never raises outward); on failure the classifiable reason is
        written to `self.last_reason` for the upper layer's `robot_alert_notify_failed_total{reason}` counter.
        """
        if alert.severity < self.min_severity:
            return False
        try:
            payload = self.build_payload(alert)
        except Exception as exc:
            log.exception("slack notify failed: %s/%s", alert.robot_id, alert.rule_id)
            self.last_reason = classify_exception(exc)
            return False

        try:
            await asyncio.to_thread(self._post_blocking, payload)
            log.info("notified %s -> slack", alert.robot_id)
            self.last_reason = None
            return True
        except Exception as exc:
            log.exception("slack notify failed: %s/%s", alert.robot_id, alert.rule_id)
            self.last_reason = classify_exception(exc)
            return False


__all__ = ["SlackNotifier", "build_slack_notifier"]


def build_slack_notifier(settings) -> SlackNotifier | None:
    """Assemble the Slack notifier from config; return None when no URL is configured (skipped)."""
    url = getattr(settings, "slack_webhook_url", "") or ""
    if not url.strip():
        return None
    return SlackNotifier(
        url=url.strip(),
        min_severity=int(getattr(settings, "alert_webhook_min_severity", 3)),
    )
