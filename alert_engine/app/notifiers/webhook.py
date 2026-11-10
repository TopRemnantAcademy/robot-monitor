"""Generic webhook notifier: a three-way adapter for DingTalk / WeCom / generic JSON.

Why "generic forward + three-way adapter" instead of pulling in `keep` directly:
- `keep` is an event-management platform (active project; `providers/` empirically contains
  slack/teams/telegram/webhook/ntfy), but has **no** dingtalk / wecom provider -- importing it would
  still require writing those two adapters ourselves, for the benefit of "one more platform to operate".
  The factory has only 100 machines; not worth it.
- DingTalk/WeCom group-bot access is both **one HTTP POST + a fixed JSON structure**,
  a 20-line job; the standard-library `urllib` suffices, with zero new dependencies.

payload shapes (per each vendor's public group-bot docs, all "text/markdown message"):

  DingTalk: {"msgtype":"markdown","markdown":{"title":T,"text":T}}
  WeCom   : {"msgtype":"markdown","markdown":{"content":C}}
  generic : {"robot_id":..,"rule_id":..,"severity":..,"message":..,"ts":..}  <- raw fields passed through

Note: DingTalk markdown uses the `text` key, WeCom uses `content` -- getting it wrong gets a 400 from
the other side, so here we construct by `kind` instead of "guessing the field".

Failure-reason classification (R-15 / GAP-3)
---------------------------
Delivery failures previously **only went to logs**; operators and Prometheus had no way to know
"notifications silently lost". This module narrows failure reasons into a **closed set of 6** (see
`NOTIFY_REASONS`), carried out by `NotifyError.reason`, for `app/main.py` to label the `reason` counter.
Only classification, no semantic change: `send()` still **never raises outward** (a notification failure
must not bring down the alert engine).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request

from app.rules.base import Alert

log = logging.getLogger("notifier")

#: The **closed classification set** of notification failures. `app/main.py` uses its values to label
#: Prometheus's `reason`, so here it is deliberately kept small and exhaustive -- an exploding label
#: cardinality (one label per distinct exception string) is a Prometheus anti-pattern, while "can't tell
#: whether the network is down or the token expired" makes the alert rule unable to suggest an action.
#:
#: - `timeout`       : connect/read timeout (rate-limited by the other side, slow network), **worth retrying**
#: - `network`       : DNS / connection refused / connection reset (`URLError` and the `OSError` catch-all), **worth retrying**
#: - `http_error`    : HTTP status non-2xx (4xx is config error, 5xx worth retrying)
#: - `business_error`: HTTP 200 but business code non-zero (DingTalk/WeCom token invalid), **retry useless**
#: - `config`        : request not yet sent locally (payload not serializable, etc.), retry useless
#: - `unknown`       : catch-all. **Not masked**: unknown reasons are still counted, just not sub-labeled
NOTIFY_REASONS = (
    "timeout",
    "network",
    "http_error",
    "business_error",
    "config",
    "unknown",
)


class NotifyError(Exception):
    """Delivery failed, carrying a **classifiable reason** (R-15 T4).

    Why carry reason via exception rather than have `send()` return a reason string:
      `send()`'s return type is an existing contract (`bool`, see tests/test_notifier.py's
      `assert ... is False` series); changing the signature would ripple to all callers and existing cases.
      The exception is a **zero-signature-change** channel for this module to pass fine-grained info from
      `_post_blocking` (sync, runs in the thread pool) across to `send()` (async); `send()` still
      translates back to bool after catching it.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        # Non-allowlisted reasons normalize to unknown: prevent someone later doing `raise NotifyError("anything")`
        # and bringing a high-cardinality label into Prometheus.
        self.reason = reason if reason in NOTIFY_REASONS else "unknown"
        self.detail = detail
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)


def classify_exception(exc: BaseException) -> str:
    """Map any exception to one of `NOTIFY_REASONS`.

    Order-sensitive: `socket.timeout` / `ssl.SSLError` are both subclasses of `OSError`,
    and must be judged before `OSError`; `urllib.error.HTTPError` is a subclass of `URLError`,
    and must be judged before `URLError` -- getting it backwards would mislabel "token expired (401)"
    as `network`, while the two have completely different ops actions (former fixes config, latter checks network).
    """
    if isinstance(exc, NotifyError):          # already classified, pass through
        return exc.reason
    if isinstance(exc, urllib.error.HTTPError):
        # Both 4xx/5xx go to http_error. Reason: the two are at the same layer in **retry strategy**
        # (both decided by `_send_with_retry`), and the benefit of subdividing is lower than the label
        # cardinality cost; while the response body itself is fully printed by webhook.py's
        # `log.exception`, those who need details go to the logs.
        # Beware: must judge before `URLError` -- `HTTPError` is its subclass.
        return "http_error"
    if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError)):
        # Note: `TimeoutError` IS `socket.timeout` (built-in alias since 3.10); the explicit form is
        # kept only to express intent; `ssl.SSLError` inherits OSError rather than ConnectionError since 3.11.
        return "timeout" if not isinstance(exc, ssl.SSLError) else "network"
    if isinstance(exc, urllib.error.URLError):
        # URLError.reason is commonly ConnectionRefusedError / socket.gaierror (DNS).
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(exc, (ConnectionError, socket.gaierror)):
        return "network"
    if isinstance(exc, OSError):
        return "network"
    if isinstance(exc, (TypeError, ValueError)):
        # json.dumps failure / illegal url, etc.: the request was never sent; retrying equals replaying the same error.
        return "config"
    return "unknown"


# severity -> display label (keep the same vocabulary on the big screen and in notifications)
_SEV_LABEL = {1: "Info", 2: "Warning", 3: "Critical"}

# Only alerts at or above this level are pushed, to avoid "low-battery storm" spam.
# Basis: this project's severity semantics 1 info / 2 warning / 3 critical (infra/init.sql:60).
_SEV_ORDER = {1: 0, 2: 1, 3: 2}


class WebhookNotifier:
    """Convert an Alert into each channel's JSON and POST it.

    `kind`: "dingtalk" | "wecom" | "generic"
    `min_severity`: alerts below this level are not pushed (default 3, i.e. only critical).
    """

    #: The failure reason of the most recent `send()` (None on success or when not sent).
    #: No label default: instance attribute rather than class attribute, to avoid cross-instance bleed.
    last_reason: str | None = None

    def __init__(self, kind: str, url: str, min_severity: int = 3,
                 timeout_s: float = 5.0) -> None:
        if kind not in ("dingtalk", "wecom", "generic"):
            raise ValueError(f"unsupported webhook kind: {kind}")
        self.kind = kind
        self.url = url
        self.min_severity = min_severity
        self.timeout_s = timeout_s
        self.last_reason = None

    # ------------------------------------------------------------- payload
    def _title(self, alert: Alert) -> str:
        return f"[{_SEV_LABEL.get(alert.severity, 'Unknown')}] {alert.rule_id} · {alert.robot_id}"

    def build_payload(self, alert: Alert) -> dict:
        title = self._title(alert)
        body = f"{title}\n\n{alert.message}"
        if alert.detail:
            # detail is optional rule context (e.g. joint id / temperature), compressed to one line for mobile reading
            body += "\n\n" + json.dumps(alert.detail, ensure_ascii=False,
                                         separators=(",", ":"))

        if self.kind == "dingtalk":
            return {"msgtype": "markdown", "markdown": {"title": title, "text": body}}
        if self.kind == "wecom":
            return {"msgtype": "markdown", "markdown": {"content": body}}
        # generic: raw fields passed through, handed to the factory's own forwarder for processing
        return {
            "robot_id": alert.robot_id,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "message": alert.message,
            "detail": alert.detail,
            "ts": None,
        }

    # --------------------------------------------------------------- send
    def _post_blocking(self, payload: dict) -> None:
        """Synchronous POST. On failure always raise `NotifyError` (with reason), **never leak the raw exception**.

        Why translate to NotifyError here: the caller (`send`) runs in the event loop and only cares
        "why did it fail", and should not re-walk urllib/socket's isinstance ladder. The classification
        logic is centralized in `classify_exception()`, testable in one place.
        """
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
            # HTTPError / URLError / socket exceptions all go down this path; classify then raise.
            raise NotifyError(classify_exception(exc), f"{type(exc).__name__}: {exc}") from exc

        # DingTalk/WeCom both return errcode:0 / errcode:0 & errmsg:ok on success;
        # HTTP 200 does not equal delivery success, the business code must still be checked.
        if code != 200:
            # urlopen throws HTTPError on 4xx/5xx (doesn't reach here), so this branch only hits on a
            # custom handler or non-standard response; still classify as http_error.
            raise NotifyError("http_error", f"webhook HTTP {code}: {raw[:200]}")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return  # treat non-JSON response as delivered (generic scenario)
        errcode = body.get("errcode", 0)
        if errcode not in (0, None):
            # business code non-zero = token invalid / rate-limited: retry useless (same token still rejected).
            raise NotifyError("business_error",
                              f"errcode={errcode}: {body.get('errmsg')}")

    async def send(self, alert: Alert) -> bool:
        """Return whether it truly delivered. Exceptions are always swallowed and logged -- a notification
        failure must not bring down the alert engine.

        The contract is unchanged (`bool`, never raises outward). The added capability is a side effect:
        on failure, write the **classifiable reason** to the log, for the upper layer's
        `robot_alert_notify_failed_total{reason}` counter.
        The classification must be finalized here -- `WebhookNotifier` is the only one that knows "why it
        failed"; if the upper layer only gets a bool, the label can only be unknown.
        """
        if _SEV_ORDER.get(alert.severity, 0) < _SEV_ORDER.get(self.min_severity, 0):
            return False
        try:
            payload = self.build_payload(alert)
        except Exception as exc:
            # Blowing up while constructing the payload (detail contains a non-JSON-able object, etc.), also never raises outward.
            log.exception("webhook notify failed (%s): %s/%s",
                          self.kind, alert.robot_id, alert.rule_id)
            self.last_reason = classify_exception(exc)
            return False

        try:
            # urllib is blocking; throw it to the thread pool to avoid blocking alert_engine's event loop
            await asyncio.to_thread(self._post_blocking, payload)
            log.info("notified %s -> %s", alert.robot_id, self.kind)
            self.last_reason = None
            return True
        except Exception as exc:
            # Beware: this `log.exception` (historically `:115` in this file) is an **existing contract**,
            # explicitly forbidden to delete by R-15 §6; this order only adds classification after it, not touching it.
            log.exception("webhook notify failed (%s): %s/%s",
                          self.kind, alert.robot_id, alert.rule_id)
            # Why store the classification result on the instance rather than in the return value: see
            # NotifyError's docstring -- `send()`'s return type is the external contract. The instance
            # attribute is a read-only aside of "most recent delivery result", not changing the call
            # protocol; `app/main.py` reads it immediately after failure.
            self.last_reason = classify_exception(exc)
            return False


__all__ = ["WebhookNotifier", "NotifyError", "NOTIFY_REASONS",
           "classify_exception", "build_notifier"]


def build_notifier(settings) -> WebhookNotifier | None:
    """Assemble the notifier from config; return None when no URL is configured (whole chain skipped)."""
    url = getattr(settings, "alert_webhook_url", "") or ""
    if not url.strip():
        return None
    return WebhookNotifier(
        kind=getattr(settings, "alert_webhook_kind", "generic"),
        url=url.strip(),
        min_severity=int(getattr(settings, "alert_webhook_min_severity", 3)),
    )
