"""R-15 / GAP-3: notification delivery failures **must be observable**
(`robot_alert_notify_failed_total{reason}`) and **bounded-retried**, while **never blocking** alert
persist/broadcast.

The object under test is the **real** notification bypass of `app.main` (`_notify` /
`_send_with_retry`) and the **real** classification logic of `app.notifiers.webhook`, not
stand-ins. Assertions come in three groups:

1. Metric and classification (V1/V2): a failure → Counter +1 per reason; success → the failure
   counter doesn't move; reason must be a value from the closed set `NOTIFY_REASONS`.
2. Doesn't block the main chain (V3, this order's **hard gate**): when the notifier **never
   returns**, `emit()` must still complete the persist and return immediately; `emit()` must not
   contain `await _notifier.send`.
3. Bounded retry (V4): count ≤3, total time has an upper bound, backoff doesn't oversleep; and
   "retry exhausted" vs "succeeded on first try" are distinguishable at the metric level.

Reverse guards at the file's end, `test_guard_*`: removing the instrumentation or making
retry infinite must turn the corresponding assertion red. Otherwise this set of cases is just
applauding an assertion that can never fail (same approach as test_store_metrics.py).
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import run

from prometheus_client import generate_latest

from app import main as engine_module
from app import metrics
from app.notifiers.webhook import NOTIFY_REASONS
from app.rules.base import Alert

FAILED = "robot_alert_notify_failed_total"
SENT = "robot_alert_notify_sent_total"


# ---------------------------------------------------------------- value helpers

def _series(text: str, name: str, labels: str = "") -> float:
    """Read the current value of a series from the Prometheus text-exposure format (0 if absent).

    Use the **public contract** `generate_latest(registry)` rather than `counter._value.get()`:
    the latter is a private py-library API. And it incidentally proves "the metric really can be
    scraped by /metrics" — if you only assert internal variables, a missing `registry=` would
    still go all green while scraping nothing online. (Same reasoning as test_store_metrics.py.)"""
    pat = rf"^{name}{re.escape(labels)} ([0-9.eE+-]+)$"
    m = re.search(pat, text, re.M)
    return float(m.group(1)) if m else 0.0


def _exposed() -> str:
    return generate_latest(metrics.registry).decode()


def _emit_and_drain(engine) -> None:
    """After `await emit()`, also run the notification tasks to completion — all in the **same**
    event loop."""
    async def _run():
        await engine.emit(_alert())
        await _wait_notify_tasks(engine)
    run(_run())


def failed(reason: str) -> float:
    return _series(_exposed(), FAILED, f'{{reason="{reason}"}}')


def sent(kind: str) -> float:
    return _series(_exposed(), SENT, f'{{kind="{kind}"}}')


def failed_all() -> dict[str, float]:
    """Failure counts for all reasons (including zero-value series, for "must stay untouched" assertions)."""
    return {r: failed(r) for r in NOTIFY_REASONS}


def _alert() -> Alert:
    return Alert(robot_id="humanoid-001", rule_id="motor_temp", severity=3,
                 message="Joint 3 temperature 82.1°C exceeds 75°C", detail={"joint": 3})


# ---------------------------------------------------------------- fake notifiers

class FailingNotifier:
    """Every `send()` fails; `last_reason` mimics webhook.py's side-note.

    `kind` is the real attribute name of `WebhookNotifier` (webhook.py:__init__), not arbitrary —
    `_notify` uses `getattr(notifier, "kind", ...)` to take the label value."""
    kind = "generic"

    def __init__(self, reason: str = "network"):
        self.last_reason = reason
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        self.last_reason = self._reason
        return False

    _reason = "network"


class RaisingNotifier(FailingNotifier):
    """A **contract-violating** bad notifier: `send()` throws directly (a normal implementation
    shouldn't).

    Used to nail `_send_with_retry`'s defense-in-depth: the exception must be swallowed inside this
    function, not bubble up through the task spawned by `spawn` into the event loop — that would
    break this order's most core invariant "notification failure doesn't affect persist/broadcast",
    and a `spawn`-ed task exception by default only prints "Task exception was never retrieved",
    making cause-and-effect extremely hard to connect."""
    async def send(self, alert) -> bool:
        self.calls += 1
        raise ConnectionError("injected: connection reset")


class HangingNotifier:
    """`send()` never returns — simulates the peer hanging (no response but no disconnect).

    This is the **only credible** posture for testing "doesn't block the main chain": making it
    return False tests "can it continue after failure", but the real risk is "when it never returns,
    does the chain still proceed"."""
    kind = "generic"
    last_reason = None

    def __init__(self):
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        import asyncio
        await asyncio.Event().wait()      # never set
        return False


class OkNotifier:
    kind = "dingtalk"

    def __init__(self):
        self.last_reason = None
        self.calls = 0

    async def send(self, alert) -> bool:
        self.calls += 1
        return True


class FlakyNotifier(FailingNotifier):
    """Succeeds starting from the `succeed_on`-th call — verifies "retry really happens and can
    recover"."""

    def __init__(self, succeed_on: int = 2):
        super().__init__("timeout")
        self._succeed_on = succeed_on

    async def send(self, alert) -> bool:
        self.calls += 1
        return self.calls >= self._succeed_on


@pytest.fixture()
def patched_notifier(engine, monkeypatch):
    """Swap `app.main._notifier` for an injectable fake notifier, and **turn off backoff wait**.

    The way to disable backoff is to set the backoff base to 0 (not patch `asyncio.sleep`):
    patching out sleep would make the "total time budget" assertion meaningless — we're testing the
    real time-axis budget control, not "how many times it slept". With base 0, `2**i * 0` stays 0,
    the budget check always passes, and the retry-count assertion then cleanly reflects only the
    "count ceiling"."""
    monkeypatch.setattr(engine_module, "NOTIFY_BACKOFF_BASE_S", 0.0)

    def install(notifier):
        monkeypatch.setattr(engine_module, "_notifier", notifier)
        return notifier

    return install


async def _wait_notify_tasks(engine) -> None:
    """Await the notification tasks spawned by `spawn()` in the **same event loop**.

    Why drain (rather than "wait a bit and look"): this order moves notifications to a background
    task (non-blocking emit), so the "failure counter +1" no longer holds true when `emit()`
    returns. The test actively awaits `engine._tasks` to **align semantics**: the assertion target
    from start to finish is the complete execution result of `_notify`. This is the only behavioral
    difference of this order from existing cases.

    ⚠️ Must await inside `run()`'s coroutine, **not** `run(emit()); run(_drain())` — two `run()`s
    are two event loops, and the task is cancelled when the first loop ends, so the second loop
    waits for nothing (measured calls stuck at 1, retry-count assertions all red)."""
    for t in [t for t in engine._tasks if not t.done()]:
        await t


# ================================================================ 1. metric exists (V1)

def test_metric_is_registered_and_exposed():
    """`robot_alert_notify_failed_total{reason}` must be scrapeable from /metrics.

    `generate_latest` emits no line for a Counter with **no sample yet** (a Counter "exists only
    with a label combination"), so here we check the HELP/TYPE declaration line — that is the
    evidence "the metric is registered". The value-assertion cases below trigger real samples."""
    text = _exposed()
    assert f"# HELP {FAILED}" in text, "counter not registered to metrics.registry"
    assert f"# TYPE {FAILED} counter" in text
    assert f"# HELP {SENT}" in text
    assert f"# TYPE {SENT} counter" in text


def test_metric_keeps_robot_alert_prefix():
    """Naming convention follows `robot_alert_*` (requirement §6 forbidden items), not a new prefix."""
    text = _exposed()
    m = re.search(rf"^# TYPE ({FAILED}\S*) ", text, re.M)
    assert m is not None
    assert m.group(1).startswith("robot_alert_")


# ================================================================ 2. failure count + classification (V2)

def test_failure_increments_counter_with_reason(engine, patched_notifier):
    """Core assertion: a delivery failure → `{reason="network"}` +1, and the success counter doesn't move."""
    n = patched_notifier(FailingNotifier("network"))
    before_failed, before_sent = failed("network"), sent("generic")

    _emit_and_drain(engine)

    assert failed("network") == before_failed + 1, f"{FAILED} did not increase with notification failure"
    assert sent("generic") == before_sent, "the failure path must not count toward notify_sent_total"
    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS, "retry count should be the ceiling"


def test_reason_label_is_classified_not_hardcoded(engine, patched_notifier):
    """Different reasons are counted separately — the label must really come from the failure
    reason, not a hardcoded constant."""
    for reason in ("timeout", "http_error", "business_error"):
        before = failed(reason)

        class _N(FailingNotifier):
            _reason = reason

        patched_notifier(_N(reason))
        _emit_and_drain(engine)
        assert failed(reason) == before + 1, f"reason={reason} not counted separately"


def test_unknown_reason_is_not_swallowed(engine, patched_notifier):
    """When the notifier gives no reason it normalizes to `unknown`, not masked (still counted)."""
    n = patched_notifier(FailingNotifier(None))
    n._reason = None
    before = failed("unknown")

    _emit_and_drain(engine)

    assert failed("unknown") == before + 1, "failures with no reason must land on unknown"


def test_success_does_not_increment_failure_counter(engine, patched_notifier):
    """Reverse assertion: on success **all** reason failure counters stay untouched (otherwise rules
    would false-alarm every day)."""
    patched_notifier(OkNotifier())
    before = failed_all()
    before_sent = sent("dingtalk")

    _emit_and_drain(engine)

    assert failed_all() == before, "success path must not increment any notify_failed counter"
    assert sent("dingtalk") == before_sent + 1, "success path must count toward notify_sent_total"


def test_raising_notifier_does_not_escape_and_is_counted(engine, patched_notifier):
    """When the notifier violates the contract and throws: exception swallowed, counted by reason,
    main chain unaffected."""
    n = patched_notifier(RaisingNotifier())
    before = failed("network")

    # If the exception escaped `_notify`, `_wait_notify_tasks` would throw it straight to the test → red
    _emit_and_drain(engine)

    assert failed("network") == before + 1
    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS


# ================================================================ 3. doesn't block main chain (V3, hard gate)

def test_notify_failure_does_not_block_save_or_publish(engine, patched_notifier):
    """Hard gate: when the notifier **never returns**, the alert still persists and `emit()` returns
    immediately.

    This is the first forbidden item of this order's §6 (notification failure must not block/rollback
    persist). Use `HangingNotifier` rather than `FailingNotifier`: only "never returns" proves the
    notification is **not** on the main chain's wait path."""
    n = patched_notifier(HangingNotifier())

    run(engine.emit(_alert()))

    # Assertion 1: persist already done (conftest's fake_save records in engine.emitted)
    assert [a.rule_id for a in engine.emitted] == ["motor_temp"], \
        "alert not persisted while notify hangs — main chain was blocked"
    # Assertion 2: the notification was actually **started** (not simply not wired)
    assert n.calls == 1, "notification task was not started"
    # Assertion 3: persist **before** any notification call (order contract)
    assert engine.emitted, "persist must be before notification"


def test_notify_not_awaited_in_emit_source():
    """Source-level guard: `emit()` must not `await` the notifier.

    The behavior assertion (the line above) depends on "the notifier happens to hang"; the source
    assertion **doesn't depend on fake behavior** — it directly forbids the line this order exists
    to remove (`await _notifier.send(alert)`) from coming back. The two are complementary: the
    behavior assertion proves "no block today", the source assertion guards "tomorrow someone
    changes it back and doesn't get caught"."""
    import inspect
    import io
    import tokenize

    # Only look at **code**, strip comments and docstrings: right above this function there's a
    # comment block explaining "changed `await _notifier.send(...)` to spawn(...)" — a raw substring
    # match over the whole source would mistake that explanation for offending code (measured).
    # Use tokenize to get the real token stream, which is "what would actually execute if run".
    src = inspect.getsource(engine_module.emit)
    code_tokens = [
        tok.string for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type not in (tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE)
    ]
    code = " ".join(code_tokens)

    assert "_notifier" in code and "await" in code, "probe broken: token stream abnormal"
    # Key: `_notifier` must not appear on the **right of `await`** (i.e. must not be awaited).
    # Equivalent in form to forbidding `await _notifier.send(...)` / `await _notifier(...)`.
    assert "await _notifier" not in code, \
        "emit() again has an awaited notification call — notification must go through spawn(_notify(...))"
    assert "spawn ( _notify ( alert ) )" in code, "emit() must dispatch notification to a background task"


def test_failsafe_when_notifier_is_none(engine, monkeypatch):
    """When no webhook is configured (`_notifier is None`): no notification, no count, no error."""
    monkeypatch.setattr(engine_module, "_notifier", None)
    before = failed_all()

    _emit_and_drain(engine)

    assert [a.rule_id for a in engine.emitted] == ["motor_temp"]
    assert failed_all() == before, "skipping notification must not produce a failure count"


# ================================================================ 4. bounded retry (V4)

def test_retry_is_bounded_by_max_attempts(engine, patched_notifier):
    """On failure, the number of attempts is **exactly** the ceiling (neither fewer nor more)."""
    n = patched_notifier(FailingNotifier("http_error"))

    _emit_and_drain(engine)

    assert n.calls == engine_module.NOTIFY_MAX_ATTEMPTS == 3


def test_retry_stops_at_first_success(engine, patched_notifier):
    """Succeed on the 2nd attempt → only called 2 times, counted as sent, not as failed."""
    n = patched_notifier(FlakyNotifier(succeed_on=2))
    before_failed = failed("timeout")
    before_sent = sent("generic")

    _emit_and_drain(engine)

    assert n.calls == 2, "must not keep retrying after success"
    assert sent("generic") == before_sent + 1
    assert failed("timeout") == before_failed, "final success must not count as failure"


def test_retry_never_exceeds_total_budget(engine, patched_notifier, monkeypatch):
    """Total time has a **hard upper bound**: even if each send eats a big chunk of budget, it must
    not overshoot by too much.

    Here let the backoff base return to its real value (0.5s) and have each `send()` take 1s,
    simulating the real time-axis of "3 attempts + 2 backoffs". The budget of 5s only constrains
    the **retry loop**, so give a scheduling margin: assert < budget + one send duration + margin.
    If the implementation changed to "infinite retry / no budget check", this case would go red
    from time blowup or count overflow."""
    import asyncio
    import time as _time

    monkeypatch.setattr(engine_module, "NOTIFY_BACKOFF_BASE_S", 0.5)

    class SlowFailing(FailingNotifier):
        async def send(self, alert) -> bool:
            self.calls += 1
            self.last_reason = "network"
            await asyncio.sleep(1.0)
            return False

    n = patched_notifier(SlowFailing())
    t0 = _time.monotonic()
    _emit_and_drain(engine)
    elapsed = _time.monotonic() - t0

    assert n.calls <= engine_module.NOTIFY_MAX_ATTEMPTS
    # worst = 3 × 1.0s(send) + 0.5 + 1.0(backoff) = 4.5s; leave 1.0s scheduling margin.
    assert elapsed < engine_module.NOTIFY_TOTAL_BUDGET_S + 1.0, \
        f"retry total time {elapsed:.2f}s exceeded budget guard"


def test_exhausted_retry_is_distinguishable_from_success(engine, patched_notifier):
    """"Recovered after retry" vs "retry exhausted" must be distinguishable at the metric level
    (that's exactly the point of adding the metric)."""
    failing = patched_notifier(FailingNotifier("business_error"))
    _emit_and_drain(engine)
    exhausted_calls = failing.calls

    flaky = patched_notifier(FlakyNotifier(succeed_on=3))
    _emit_and_drain(engine)

    assert exhausted_calls == 3                    # exhausted: 3 all fail
    assert flaky.calls == 3                        # recovered: succeeds on 3rd
    # Same attempt count, but one went to failure count, one to success count — metrics can tell.
    assert failed("business_error") > 0
    assert sent("generic") > 0


# ================================================================ 5. classification function (webhook side)

def test_classify_maps_http_and_network_and_timeout():
    """The mapping table of `classify_exception` — the **only** source of the reason label."""
    import socket
    import urllib.error

    from app.notifiers.webhook import classify_exception

    assert classify_exception(
        urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)) == "http_error"
    assert classify_exception(socket.timeout("t")) == "timeout"
    assert classify_exception(
        urllib.error.URLError(socket.timeout("t"))) == "timeout"
    assert classify_exception(
        urllib.error.URLError(ConnectionRefusedError("refused"))) == "network"
    assert classify_exception(ConnectionError("reset")) == "network"
    assert classify_exception(TypeError("not serializable")) == "config"
    assert classify_exception(RuntimeError("???")) == "unknown"


def test_classify_output_is_always_a_declared_reason():
    """Closed set: any input must produce a value from `NOTIFY_REASONS` (prevents label-cardinality blowup)."""
    from app.notifiers.webhook import classify_exception

    for exc in (ValueError("x"), OSError("x"), RuntimeError("x"),
                KeyError("x"), Exception("x")):
        assert classify_exception(exc) in NOTIFY_REASONS
    assert "unknown" in NOTIFY_REASONS, "must have a fallback value, otherwise exceptions are silently dropped"


def test_notify_error_normalises_unknown_reason():
    """`NotifyError` must normalize a non-allowlisted reason to unknown."""
    from app.notifiers.webhook import NotifyError

    assert NotifyError("random-string").reason == "unknown"
    assert NotifyError("timeout").reason == "timeout"


def test_webhook_send_records_reason_on_failure():
    """The real `WebhookNotifier.send()` must leave the reason side-noted on the instance on failure.

    This is the **seam** between "classification" and "counting": `app/main.py` reads exactly this
    attribute. Testing only the classification function without testing this attribute, the link
    could break (e.g. a rename) and both sides stay green while everything in production lands on
    unknown."""
    from unittest import mock

    from app.notifiers.webhook import WebhookNotifier

    import urllib.error

    n = WebhookNotifier("generic", "http://x")
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.URLError(ConnectionRefusedError("x"))):
        ok = run(n.send(_alert()))

    assert ok is False
    assert n.last_reason == "network", "must leave a classifiable reason after failure"


def test_webhook_send_keeps_existing_log_line(capsys=None):
    """The existing log contract (webhook.py originally :115) must not be deleted — requirement §6 forbidden item.

    Source-level assertion (rather than capturing the log): it guards against both "changing the
    string" and "changing the log level"."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app/notifiers/webhook.py"
    text = src.read_text(encoding="utf-8")
    assert 'log.exception("webhook notify failed (%s): %s/%s",' in text, \
        "webhook.py's existing log.exception was deleted/changed (explicitly retained by R-15 §6)"


# ================================================================ 6. reverse guards

def test_guard_detects_a_notify_path_that_forgets_to_count(engine, patched_notifier,
                                                           monkeypatch):
    """Remove the instrumentation to a no-op (= the old code's "log only" behavior), and the counting assertion
    must go red.

    Meta-test: proves the assertions above really watch "is there an inc()", not some incidental
    value change.

    Use a **single reason** (`config`, produced by no other case in this file) rather than a
    `failed_all()` full comparison: Prometheus's Counter is **process-global**, and samples left by
    other cases in the same process come along. Measured: `failed_all()`'s snapshot had `network`
    go 5→6, exactly this case's own leftover from the previous run, unrelated to the instrumentation. A
    single-point assertion narrows "the object under test" to a point guaranteed not to be
    polluted."""
    patched_notifier(FailingNotifier("config"))
    monkeypatch.setattr(metrics.notify_failed, "inc", lambda *a, **kw: None)

    before = failed("config")
    _emit_and_drain(engine)

    assert failed("config") == before, \
        "still counting after the instrumentation was removed — assertion isn't on the instrumentation"


def test_guard_detects_unbounded_retry(engine, patched_notifier, monkeypatch):
    """Bump the count ceiling to 10, and the count assertion must go red — proves "≤3" is really tested.

    Use 10 rather than a truly infinite loop: an infinite loop in a test is a hang, not a failure, and would be
    misjudged by CI timeout as an environment issue. Bumping to "clearly above the ceiling" suffices
    to falsify."""
    monkeypatch.setattr(engine_module, "NOTIFY_MAX_ATTEMPTS", 10)
    n = patched_notifier(FailingNotifier("network"))

    _emit_and_drain(engine)

    assert n.calls == 10 != 3, "after the ceiling was changed, the count didn't change — count assertion is fake"
