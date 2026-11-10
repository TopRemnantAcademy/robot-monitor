"""Alert engine entry point (spec/02 §5).

Two driver lines:
1. **Consume line**: subscribe to Redis `robot:stream:all`, maintain a sliding window per robot,
   and run the motor_temp / battery_low / joint_stuck rules in sequence.
2. **Ticker line**: every second iterate 100 robots and run the offline rule.
   The criterion **prefers** `robot_metadata.is_online` (written by the backend metadata_writer
   heartbeat, ISSUE #96); when that column is unavailable, fall back to the in-memory last_seen time
   delta, to avoid false alarms early in deployment.
   -- **Does not depend on Redis Key expiration events** (keyspace notification is off by default).

Alerts go through **three layers of filtering**, in the fixed order **silence -> inhibition -> dedup**
(see `emit()`'s docstring for the reason), then store writes robot_alert and PUBLISHes robot:alerts.

There is also an **escalation line** (order 17 T2): periodically scans alerts that are
"severity=3 and unacked past timeout", marks them escalated and re-sends, matching Alertmanager's
`repeat_interval` semantics.

Notification (DingTalk/WeCom/generic webhook) is a **side path** (R-15 / GAP-3): persistence and WS
broadcast complete first, then the notification is given **bounded retries** (<=3 attempts, total <=5s,
exponential backoff); on total failure count `robot_alert_notify_failed_total{reason}`. A notification
failure **never** blocks, rolls back, or delays alert persistence -- an external system's failure should
not make this system lose data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque

from redis.exceptions import RedisError

from app.config import settings
from app.db import close_all, get_redis
from app.deduplicator import should_emit
from app.inhibition import (
    MIN_SOURCE_SEVERITY,
    ActiveAlertRegistry,
    InhibitionRecord,
    evaluate_severity_inhibition,
)
from app.escalation import escalation_loop
from app.silence import is_silenced
from app.rules.battery import BatteryLowRule
from app.rules.joint_stuck import JointStuckRule
from app.rules.motor_temp import MotorTempRule
from app.rules.offline import OfflineRule
from app.rules.anomaly import AnomalyRule
from app.rules.base import Alert
from app import metrics
from app.notifiers import build_notifier, build_slack_notifier
from app.notifiers.webhook import classify_exception
from app.store import load_online_state, save, save_suppressed, update_online

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("alert_engine")

RULES = [MotorTempRule(), BatteryLowRule(), JointStuckRule(), OfflineRule(), AnomalyRule()]
DEDUP_WINDOW = {r.rule_id: r.dedup_window_s for r in RULES}
STREAM_RULES = [r for r in RULES if r.rule_id != "offline"]
OFFLINE_RULE = next(r for r in RULES if r.rule_id == "offline")

# The **source rule** for inhibition: when an offline alert is active, inhibit this robot's other alerts.
# Corresponds to Alertmanager's `inhibit_rules`: source=offline / equal=[robot_id].
INHIBIT_SOURCE_RULE = "offline"

windows: dict[str, deque] = {}
# last_seen is still maintained by the consume line, as the **fallback** criterion for offline
# (ISSUE #96: when the column is unavailable)
# and for the alert text ("no telemetry for Ns").
last_seen: dict[str, float] = {}
# The online state inferred by this engine from in-memory last_seen, still written to robot_metadata
# at low frequency on state changes (compatible with #61's existing path); also used by inhibition (is_inhibited).
online_state: dict[str, bool] = {}

# Per-robot active-alert registry (order 30) -- the criterion source for severity inhibition.
# Same source as online_state: in-process derived state; see app/inhibition.py's notes.
active_alerts = ActiveAlertRegistry()

_tasks: set[asyncio.Task] = set()

# Notifier: None when no URL is configured (whole chain silently skipped).
# Assembling at module level is a deliberate fail-fast: a misspelled kind prevents the process from
# starting, which is better than misconfiguring and silently not notifying at runtime.
_notifier = build_notifier(settings)
# Slack notifier (GitHub issue #8): None when SLACK_WEBHOOK_URL is empty. Both notifiers are tried
# independently in `_notify`; a failure on one does not affect delivery on the other.
_slack_notifier = build_slack_notifier(settings)

# ---- Notification retry limits (R-15 T3) ------------------------------------------------
# The values are **hard caps**, not tunable parameters: requirement §6 explicitly forbids "unbounded
# retries / unbounded waiting", and putting them in config would give the chance to "tune it to 100
# attempts", which is not the freedom this order wants.
#   3 attempts: covers transient faults like "self-heals one second after rate-limiting"; more is
#     meaningless for a group bot.
#   5s: same order of magnitude as `WebhookNotifier.timeout_s` (single HTTP timeout) -- i.e. the worst
#     case single-attempt time budget; total budget = 3 x single budget, calculable and not out of control.
# Exponential backoff base 0.5s -> 0.5 / 1.0 (only two intervals), total sleep 1.5s, 30% of the budget.
NOTIFY_MAX_ATTEMPTS = 3
NOTIFY_TOTAL_BUDGET_S = 5.0
NOTIFY_BACKOFF_BASE_S = 0.5


def spawn(coro) -> None:
    """Run alert persistence/publish in the background without blocking the consume loop; also keep a reference to prevent GC."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def inhibition_reason(alert: Alert) -> InhibitionRecord | None:
    """Inhibition decision; returns a **traceable receipt**; returns None to pass (order 30: forbid silent dropping).

    Two levels of decision; a hit at either one inhibits (with a different `reason` for audit attribution):

    1. **offline layer** (order 10 existing): robot already judged offline -> inhibit all derivative
       alerts on the same machine. `reason="offline"`.
    2. **severity layer** (order 30 new): a higher-severity active alert exists on the same machine ->
       inhibit this one. `reason="severity"`.

    Why keep offline separate rather than merging into the severity layer: offline is the "whole machine
    lost contact" **absolute root cause**, whose semantics is "any derivative alert is worthless"; the
    severity layer is relative suppression. Merging would make "why inhibit battery_low when offline" and
    "why inhibit low battery when overheating" isomorphic on the metrics, and operators could not tell
    whether it was a dropout or overheating. The two `reason`s must be distinguishable.

    Two key invariants (getting them wrong swallows real alerts):

    1. `online_state.get(rid) is False` -- must be an explicit `is False`,
       not `not online_state.get(rid)`. The ticker must run a full round before filling values for the
       100 robots; early at startup the dict is empty, and a falsy judgment would mark **all** alerts as inhibited.
    2. **The inhibition source itself is never inhibited** -- the offline rule is not suppressed by the
       offline layer; the severity layer uses `exclude_rule` to exclude itself and requires the source to
       be strictly higher. Otherwise the root-cause alert is never reported.
    """
    rid = alert.robot_id

    # ---- Level 1: offline ----
    if settings.inhibit_offline and alert.rule_id != INHIBIT_SOURCE_RULE:
        if online_state.get(rid) is False:
            return InhibitionRecord(
                robot_id=rid,
                target_rule=alert.rule_id,
                target_severity=alert.severity,
                source_rule=INHIBIT_SOURCE_RULE,
                source_severity=3,
                reason="offline",
            )

    # ---- Level 2: severity (higher severity inhibits lower on the same machine) ----
    if settings.inhibit_severity:
        rec = evaluate_severity_inhibition(
            alert, active_alerts, min_source_severity=MIN_SOURCE_SEVERITY
        )
        if rec is not None:
            return rec

    return None


def is_inhibited(alert: Alert) -> bool:
    """Boolean facade for the inhibition decision (keep existing caller/test compatibility).

    Implementation delegates to `inhibition_reason()`; call the latter directly when the receipt is needed.
    """
    return inhibition_reason(alert) is not None


# ---------------------------------------------------------------- notification (side path)

async def _send_with_retry(notifier, alert: Alert) -> tuple[bool, str | None]:
    """Bounded-retry delivery. Returns `(success, failure reason)`.

    The caps come from module constants (see their comments): **at most `NOTIFY_MAX_ATTEMPTS` attempts**,
    and **total time not exceeding `NOTIFY_TOTAL_BUDGET_S`**. Both caps are needed, because attempts
    alone are insufficient -- each HTTP carries its own `timeout_s`, and attempts x single timeout is the
    worst-case time; time alone is insufficient -- after the budget is exhausted we must be able to say
    "tried N times". Backoff is exponential (`base * 2**i`), and the remaining budget is **recomputed
    before each sleep**, ensuring `asyncio.sleep` never pushes the total time over (sleeping "too long"
    is the gentle version of unbounded waiting, also forbidden by requirement §6).
    """
    started = time.monotonic()
    last_reason: str | None = None

    for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
        try:
            if await notifier.send(alert):
                return True, None
        except Exception as exc:
            # The notifier contract is "never raises outward" (webhook.py:send has its own try/except),
            # this extra layer is **defense in depth**: if one ever leaks, the exception would bubble up
            # through the `spawn` task to the event loop and break this order's core invariant
            # "notification failure does not affect persistence/broadcast". The cost is one except line.
            log.exception("notifier raised despite contract: %s/%s",
                          alert.robot_id, alert.rule_id)
            last_reason = classify_notify_reason(exc)
        else:
            # Taking the else branch = send() returned False normally (already swallowed the exception
            # internally); reason is annotated by the notifier on `last_reason` (see webhook.py's notes).
            last_reason = getattr(notifier, "last_reason", None) or "unknown"

        if attempt == NOTIFY_MAX_ATTEMPTS:
            break

        backoff = NOTIFY_BACKOFF_BASE_S * (2 ** (attempt - 1))
        remaining = NOTIFY_TOTAL_BUDGET_S - (time.monotonic() - started)
        if remaining <= backoff:
            # Budget insufficient for another backoff round -> give up retrying, count directly with the
            # current reason. This is not "early failure" but "waiting longer would exceed the budget",
            # hence break rather than sleep(0).
            log.warning("notify retry budget exhausted after %d attempt(s) on %s/%s",
                        attempt, alert.robot_id, alert.rule_id)
            break
        await asyncio.sleep(backoff)

    return False, last_reason


def classify_notify_reason(exc: BaseException) -> str:
    """Classify an exception unexpectedly raised by the notifier.

    On the normal path `webhook.py:send()` never raises outward; reason is given by the notifier itself;
    this function only serves `_send_with_retry`'s fallback branch -- but it **cannot be omitted**:
    the fallback branch must also produce a legal label, otherwise `metrics.notify_failed.labels(reason=...)`
    would be fed high-cardinality values like exception class names. Delegates to the notifier's same
    classification table (`classify_exception`), guaranteeing "who classifies" has only one implementation.
    """
    return classify_exception(exc)


async def _notify(alert: Alert) -> None:
    """Notification **side-path task**: delivery + counting; on failure only record metric/log, never raise outward.

    Why a "background task" in the TODO rather than `await` inside `emit()` (key decision):
      `emit()` runs in the consume task spawned by `spawn()`, and its **next thing** is to go back to
      `pubsub.listen()` for the next message. If we await the notification here (even bounded retries),
      the worst case is that this coroutine cannot fetch messages for 5s -- a 100-machine @10Hz stream
      would pile up in the Redis buffer, and "slow notification" (external system) thus becomes
      "slow alert consumption" (this system).
      This order's hard gate is **notification failure must not block alert persistence/broadcast**;
      moving the notification entirely out of the main chain turns this gate from "best effort" into
      "structurally impossible to violate".
      Cost: one alert's notification and its persistence no longer complete at strictly the same instant,
      acceptable -- notification is inherently "best effort" (requirement §3).
    """
    # Take a snapshot before await: during retries the notifier set is stable here, but we still read
    # it once so a future monkeypatch of `_notifier` (tests) is honored.
    notifiers = [n for n in (_notifier, _slack_notifier) if n]
    if not notifiers:
        return
    for notifier in notifiers:
        try:
            ok, reason = await _send_with_retry(notifier, alert)
        except asyncio.CancelledError:
            raise                     # cancel is control flow, not delivery failure; must not be counted as failure
        except Exception as exc:      # pragma: no cover - defense in depth, normally unreachable
            log.exception("notify task crashed: %s/%s", alert.robot_id, alert.rule_id)
            ok, reason = False, classify_notify_reason(exc)

        if ok:
            metrics.notify_sent.labels(kind=getattr(notifier, "kind", "unknown")).inc()
            continue

        # Failure: the count must land within the **closed reason set** (`NotifyError` already normalized,
        # here we guard None/empty string once more, to prevent an empty label).
        metrics.notify_failed.labels(reason=reason or "unknown").inc()
        # The failure has already been printed per-attempt by the notifier's `log.exception`; here we add a
        # **conclusion-level** log line: record "finally gave up, retried N times", otherwise the log would
        # only have 3 identical failure stacks, and the reader could not distinguish "failed but retried
        # successfully" from "totally failed" (metrics can distinguish, human reading logs cannot).
        log.error("notify giving up after retries: reason=%s %s/%s",
                  reason or "unknown", alert.robot_id, alert.rule_id)


async def emit(alert: Alert) -> None:
    """Alert exit. Three-layer filtering, **order must not be swapped**: silence -> inhibition -> dedup.

    Why this order (order 17 T3 requires the reasons written out):

    1. **silence first**. Silence is an operator-explicit "do not disturb me right now" command,
       higher priority than any automatic judgment. If placed after inhibition/dedup, two consequences:
       - silenced alerts would still **write the dedup Key**, so within the first second after silence is
         lifted, the alert that should have fired is "secondarily suppressed" by the dedup window left
         during the silence -- silence produces an after-effect it should not have semantically.
       - the intent of silence is "do not process", while inhibition/dedup both need to read state first
         (Redis/in-memory), one more needless judgment.
    2. **inhibition second**. Inhibition is "root cause known, derivative alerts worthless" (when a robot
       is offline, low battery / joint stuck need not be reported). It must be **before** dedup, otherwise
       derivative alerts would occupy the dedup window first, and after the root cause recovers the real
       alert might be blocked by dedup -- order 10's original design was like this (`emit` had inhibition
       before `should_emit`), this order keeps it.
    3. **dedup last**. The dedup window is a one-time "counting side effect" (writing a Redis Key);
       only when it is determined that this alert really must be sent does it deserve to consume the window.
       The first two layers should not waste it.

    In other words: silence decides "whether to look", inhibition decides "whether this one has value",
    dedup decides "whether this window already sent one". From coarse to fine, each layer only handles
    the alerts "passed by the layer above".
    """
    if await is_silenced(alert):
        # Silence counting is a separate metric: same as inhibition, the **drop** in `alerts_emitted`
        # must be attributable -- was it silenced or inhibited? The ops actions are completely different.
        metrics.alerts_silenced.labels(rule_id=alert.rule_id).inc()
        log.debug("alert silenced: %s %s (silence key active)", alert.robot_id,
                  alert.rule_id)
        return
    # Ask the boolean facade first (keep the existing monkeypatch seam), fetch the receipt on a hit.
    # Two steps rather than a direct `inhibition_reason()`: `is_inhibited` is the stable seam for
    # order 10/17/24 tests and external contracts; bypassing it directly would invalidate existing spies.
    if is_inhibited(alert):
        record = inhibition_reason(alert)
        # Inhibition counting must be separate: otherwise the **drop** in `alerts_emitted` cannot be
        # attributed -- did the robot actually become healthy, or was it blocked by the inhibition layer?
        # The two have completely different ops meanings.
        # Order 30 added the `reason` dimension: offline and severity have different ops actions
        # (the former checks network/power, the latter checks whether the root-cause alert was handled).
        # record may be None: only happens when `is_inhibited` is monkeypatch'd to always-true externally
        # while the real decision passes (test seam scenario). Degrade to reason="unknown", neither
        # masking the call nor throwing to interrupt the alert chain.
        reason = record.reason if record is not None else "unknown"
        metrics.alerts_suppressed.labels(
            rule_id=alert.rule_id, reason=reason
        ).inc()
        # Forbid silent dropping: DEBUG is for "those who can get the logs"; ops at INFO level / the alert
        # console cannot see DEBUG. Hence the inhibition receipt is output as a single WARNING line,
        # containing source and target, directly grep-able via `inhibition` to trace "why didn't this go out".
        log.warning(
            "alert inhibited: %s",
            record.as_log_fields() if record is not None
            else f"reason=unknown robot={alert.robot_id} target={alert.rule_id}(sev{alert.severity})",
        )
        # Order 35: **persist** the inhibition receipt (no WS push, no notification), so `GET /alerts` can query it.
        # Order 30's "no silent dropping" previously only went as far as logs and metrics -- the alert
        # console could not see the suppressed row; the on-duty staff had to switch to the log system to
        # answer "what else did this machine report at the time".
        # Side-path semantics: persistence failure only records metric+log (see store.save_suppressed),
        # does not affect the main chain; same as `save()`, spawn into a background task, does not block
        # the consume line.
        if record is not None:
            spawn(save_suppressed(alert, record))
        return
    window = DEDUP_WINDOW.get(alert.rule_id, 60)
    if await should_emit(alert, window):
        await save(alert)
        # Register as an active alert (order 30): this one has been sent, so it becomes a candidate
        # inhibition source for this machine's later lower-severity alerts. Placed after save -- only
        # alerts that truly went out deserve to be the root cause.
        active_alerts.register(alert)
        metrics.alerts_emitted.labels(rule_id=alert.rule_id).inc()
        log.info("alert: %s %s sev=%d %s", alert.robot_id, alert.rule_id,
                 alert.severity, alert.message)
        # Push to the factory-side group bot (DingTalk/WeCom/generic).
        # Placed after save: persist first to guarantee audit, then notify. Suppressed alerts already
        # returned above, will not reach here.
        #
        # R-15 (GAP-3): changed from `await _notifier.send(...)` to `spawn(_notify(...))`,
        # reason see `_notify`'s docstring -- the wait on the notification chain (including retries) must
        # never occupy the alert consume loop. Failure terminates inside `_notify` (only count + log),
        # structurally impossible to roll back or block the already-completed `save()` / broadcast above.
        if _notifier is not None or _slack_notifier is not None:
            spawn(_notify(alert))


# ---------------------------------------------------------------- consume line

# The **disconnection-class** exception tuple for `pubsub.listen()` (R-14).
#
# Beware: here we must list both `RedisError` and the built-in `ConnectionError/OSError/TimeoutError`,
# the two **do not form an inheritance relationship**: local measurement (redis 5.0.3)
#     >>> issubclass(redis.exceptions.ConnectionError, ConnectionError)  # built-in
#     False
#     >>> issubclass(redis.exceptions.TimeoutError, TimeoutError)        # built-in
#     False
# redis-py's own `exceptions.ConnectionError` / `TimeoutError` only inherit
# `RedisError -> Exception`, they are **same-named but unrelated** classes to the built-in ones. Catching
# only the built-in `ConnectionError` would miss the one redis driver actually throws, equal to not fixing;
# catching only `RedisError` would miss the underlying socket-directly-thrown `OSError` (hiredis path).
# So both groups are listed.
# Use a tuple rather than stacking `except` inside `consume()`: catch only once, avoid the implicit
# order dependency of "which except hits first".
REDIS_DISCONNECT_ERRORS = (RedisError, ConnectionError, OSError, TimeoutError)

# Reconnect backoff params (seconds): start at 1s, double each time, cap at 30s.
# Why not double without bound: the 100-machine x 10Hz downstream is a **bounded** load, and backoff
# only needs to dodge the "Redis is restarting" jitter window; capping at 30s guarantees that at worst
# 30s after Redis recovers it self-heals, and will not become a "de facto permanent unsubscribe" because
# backoff grew to hours (which would be no different from not fixing).
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0


async def consume() -> None:
    """Consume `robot:stream:all`, with **in-coroutine** backoff reconnect on Redis disconnection (R-14).

    Before R-14 the structure was `subscribe` then directly `async for msg in pubsub.listen()`,
    and the disconnection exception thrown by `listen()` would escape this coroutine -> propagate upward
    through `amain()`'s `asyncio.gather` -> the entire coroutine tree fails and the process exits; even
    though the container has restart, `ticker()` and `escalation_loop()` -- the two sibling coroutines
    that were **not broken themselves** -- get cancelled by the same gather.

    Now changed to a "self-restart" structure: the outer `while True` never exits due to disconnection,
    the inner only runs one round of listening. On disconnection, swallow the exception, back off
    exponentially, **rebuild pubsub and re-subscribe** (subscription state is not retained across
    connections; missing this step means silently not consuming), then `continue` to the next round.

    Iron rule: the exception must **not** escape this coroutine (§6), and must **not** re-`raise` the
    reconnect failure (that would equal not fixing).
    """
    backoff = RECONNECT_BACKOFF_INITIAL_S
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(settings.chan_stream_all)
            log.info("alert_engine consuming %s", settings.chan_stream_all)
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                # Backoff reset point: the moment of **receiving a real message** is the only hard
                # evidence that "this link is really up", so the reset must hang here, and only here:
                #   - if hung after `subscribe()` returns: when Redis is restarting, subscribe can
                #     succeed, then immediately disconnect, and backoff would stay at 1s forever ->
                #     the "exponential backoff" exists in name only, degrading into a busy retry storm
                #     every second (local measurement: 6 consecutive disconnection backoff sequences all
                #     at the initial value).
                #   - if hung after `async for` ends normally: those rounds that end in disconnection
                #     never reach that line, and "disconnect after having delivered messages" is exactly
                #     the case that most deserves a reset.
                backoff = RECONNECT_BACKOFF_INITIAL_S
                try:
                    data = json.loads(msg["data"])
                except json.JSONDecodeError:
                    continue
                rid = data.get("robot_id")
                if not rid:
                    continue

                w = windows.setdefault(rid, deque(maxlen=settings.window_maxlen))
                w.append(data)
                last_seen[rid] = time.time()

                for rule in STREAM_RULES:
                    try:
                        alert = rule.evaluate(data, w)
                    except Exception:
                        log.exception("rule %s failed on %s", rule.rule_id, rid)
                        continue
                    if alert is not None:
                        spawn(emit(alert))
                    else:
                        # This round the rule did not hit = recovered -> clear registration, otherwise
                        # it would forever act as the "root cause" suppressing this machine's other alerts
                        # (order 30 key point: not clearing in time = permanent false inhibition).
                        active_alerts.clear(rid, rule.rule_id)
        except REDIS_DISCONNECT_ERRORS as exc:
            # Only swallow disconnection-class exceptions; other exceptions (real bugs) escape as usual,
            # not masked here into "fake alive".
            # Note the except hangs inside `while True`, wrapping **the whole round of listening** (including
            # listen() and message processing), rather than nested inside message processing -- otherwise
            # the disconnection thrown by listen() itself would have no fallback (this is exactly R-14's hole).
            metrics.redis_reconnect.inc()
            log.warning(
                "consume(): Redis disconnected (%s: %s), reconnect + re-subscribe %s in %.1fs",
                type(exc).__name__, exc, settings.chan_stream_all, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_S)
            continue


# ---------------------------------------------------------------- ticker line

async def ticker() -> None:
    """One round per second: run the offline rule (criterion prefers robot_metadata.is_online) + maintain online state.

    Why read the DB once first (ISSUE #96)
    -----------------------------
    The criterion must be "has the backend seen this robot", not "the last_seen recorded in this
    process's memory". The latter would quietly distort on this engine's restart / Redis pubsub
    disconnection (memory cleared -> 100 machines collectively judged offline). So each round first
    `load_online_state()` fetches 100 rows, then judges offline per machine: **one** DB round trip per
    round, not amplified by machine count.

    When unreadable (returns {}), the `is_online` passed to the rule is None -> the rule falls back to
    the last_seen time-delta judgment (migration-period safety net, see `app/rules/offline.py`).
    """
    while True:
        await asyncio.sleep(settings.tick_interval_s)
        now = time.time()
        db_state = await load_online_state()
        for rid in settings.robot_ids:
            ls = last_seen.get(rid)
            w = windows.get(rid) or deque()
            is_online = db_state.get(rid)      # missing row / DB read failure -> None (fallback)
            try:
                alert = OFFLINE_RULE.evaluate(
                    {"robot_id": rid, "is_online": is_online, "last_seen": ls}, w
                )
            except Exception:
                log.exception("offline rule failed on %s", rid)
                alert = None
            if alert is not None:
                spawn(emit(alert))
            else:
                # No longer judged offline = recovered -> clear offline registration (otherwise, as a
                # sev3 inhibition source, it would keep suppressing this machine's alerts). Order 30.
                active_alerts.clear(rid, INHIBIT_SOURCE_RULE)

            online = ls is not None and (now - ls) <= settings.offline_after_s
            if online_state.get(rid) is not online:
                online_state[rid] = online
                spawn(update_online(rid, online))
                if not online:
                    # Judged offline -> clear this machine's **derivative** alert registration: at this
                    # moment they have no remediation value, keeping them only overlaps with the offline
                    # inhibition semantics and pollutes the severity layer's criterion.
                    # offline's own registration is written by the emit above after inhibition passes.
                    active_alerts.clear_robot(rid)

        # Under the pull model the Gauge must be refreshed every round: no one sets it periodically for
        # us; without refresh the scraped value is forever the stale "moment of last state change".
        metrics.robots_online.set(sum(1 for v in online_state.values() if v))


def _isolate(name: str, coro):
    """Wrap a driver line into a coroutine that "crashes only logs, does not overturn siblings" (R-14 / T3).

    Why it is needed: `asyncio.gather(consume(), ticker(), escalation_loop())`'s default semantics is
    **any child coroutine throws -> gather immediately raises, the rest of the child coroutines are
    cancelled**. Thus one Redis blip in `consume()` can kill `ticker()` (offline detection) and
    `escalation_loop()` (escalation scan) -- both not broken themselves. R-14 already catches
    disconnection-class exceptions inside `consume()`; this function is the second layer of isolation
    against **other non-disconnection unexpected crashes**.

    Trade-off (must be written out, otherwise this is hiding the fault): gather's default "fail fast +
    cancel siblings" is **semantically wrong** here -- the three lines are **mutually independent driver
    lines**, one going down is not a reason the other two should stop; container restart is the correct
    level for "overall restart". The cost is the process may "fake-live" (one line died but the process
    is still up). Therefore here we **must** guarantee the crash is visible: `log.critical` + `traceback`,
    so the log alert beyond `AlertEngineDown` can catch it.
    Only catch `Exception`, not `BaseException`: `CancelledError` must still be raised, otherwise when
    `amain()`'s `finally` cancels `_tasks` it would be swallowed here and not shut down cleanly.
    """
    async def _wrapped():
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.critical("Driver line %s crashed unexpectedly (its sibling coroutines are unaffected)", name,
                         exc_info=True)
            return None
    return _wrapped()


async def amain() -> None:
    try:
        # escalation_loop runs concurrently with the other two driver lines: it does not correspond to an
        # "input stream" but periodically scans alerts that are "sent, severity=3, unacked past timeout",
        # re-sends escalation (order 17 T2).
        # The three lines are each isolated (R-14 / T3): one crashing no longer cancels the other two.
        await asyncio.gather(
            _isolate("consume", consume()),
            _isolate("ticker", ticker()),
            _isolate("escalation_loop", escalation_loop()),
        )
    finally:
        for t in list(_tasks):
            t.cancel()
        await asyncio.gather(*_tasks, return_exceptions=True)
        await close_all()


def main() -> None:
    # Start the metrics HTTP server first (separate thread, stdlib http.server),
    # to guarantee Prometheus always gets up=1 whenever it scrapes, rather than waiting for the asyncio
    # loop to start first.
    # This service is not FastAPI and has no route to mount /metrics on, so it can only start an independent port.
    server = metrics.start(settings.metrics_port)
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("alert_engine stopped")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
