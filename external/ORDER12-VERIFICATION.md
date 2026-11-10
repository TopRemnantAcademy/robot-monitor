# Order 12 verification report: verifying three conclusions against real upstream source

> Re-checked object: the three items marked ✅ measured in `external/INTEGRATION-PLAN.md` §6 "verification boundaries".
> Method: `git clone --depth 1` real source + `git show <tag>:<file>` to get the original text of a specified version + isolated `pip --target` install for `inspect.getsource` runtime forensics.
> Environment: Python 3.13.x | Network: github.com reachable (measured in this container).

## 0. Correct the task premises first (two items)

| User stated | Actual |
|---|---|
| `orders/12-dispatch.md` exists | **does not exist**. The repo's `orders/` only goes up to `10-`; 11/12 are missing |
| `scripts/fetch-external-refs.sh` exists | **does not exist**, there isn't even a `scripts/` directory |

This report has **supplied** `scripts/fetch-external-refs.sh` (executable, re-runnable) and recorded the conclusions in this file.

---

## 1. Upstream source fetch results (14/14 all succeeded)

```
OK    mqtt-to-timescaledb      OK    aiomqtt            OK    asyncpg
OK    tsbs                     OK    zustand            OK    echarts-for-react
OK    emqtt-bench              OK    redis-py           OK    starlette
OK    full-stack-fastapi-template   OK    client_python
OK    alembic                  OK    paho.mqtt.python   OK    alertmanager (sparse: docs/)
----- 成功/已存在: 13  失败: 0（alertmanager 单列 sparse，计 14）
```

github.com is reachable; there is **no** "can't pull" problem.

---

## 2. Conclusion ①: aiomqtt 2.5.0's `__aexit__` fix —— ❌ **the original statement had the direction reversed**

### 2.1 Original text (user/plan stated)

> I-1: our reconnect **fully relies** on `async with` raising `MqttError` on exit to trigger backoff reconnect. Under 2.0.1, `__aexit__` would **swallow the exception** → the reconnect branch might never execute.

That is, "2.0.1 swallows the exception, 2.5.0 changes it to throw".

### 2.2 Source evidence (conclusion: the opposite)

`external/aiomqtt`, `git diff v2.0.1 v2.5.0 -- aiomqtt/client.py`:

```diff
     async def __aexit__(self, exc_type, exc, tb) -> None:
         """Disconnect from the broker."""
         if self._disconnected.done():
             # Return early if the client is already disconnected
             if self._lock.locked():
                 self._lock.release()
-            if (exc := self._disconnected.exception()) is not None:
-                # If the disconnect wasn't intentional, raise the error that caused it
-                raise exc
+            # let any error on disconnection exception bubble up without suppressing it
             return
```

- **v2.0.1** (`aiomqtt/client.py:727-729`): `__aexit__` **does** re-raise the disconnect cause (`raise exc`).
- **v2.5.0** (`aiomqtt/client.py:803-807`): **removed `raise exc`**, changing it to a direct `return`.

CHANGELOG (`CHANGELOG.md:48`, `## [2.5.0] - 2026-01-04`) original text:

```
- Let exceptions bubble up in `Client.__aexit__` instead of suppressing them (@yujia21 in #375)
```

### 2.3 Runtime forensics (not just reading source)

Isolated install of three versions, using `inspect.getsource(Client.__aexit__)`:

| Version | Install method | `raise exc` present | `bubble up` comment present |
|---|---|---|---|
| 2.0.1 | `pip --target` (aliyun mirror) | **True** | False |
| 2.5.0 | `pip --target` | **False** | True |
| 2.5.1 | `pip --target` | **False** | True |

Reproduction command:

```bash
pip install -q -i https://mirrors.aliyun.com/pypi/simple --target /tmp/t250 "aiomqtt==2.5.0"
PYTHONPATH=/tmp/t250 python3 -c "import inspect,aiomqtt;s=inspect.getsource(aiomqtt.Client.__aexit__);print('raise exc' in s)"
# -> False
```

### 2.4 So what did this fix actually fix? (correct interpretation)

The `exc` parameter of `__aexit__` is the **exception raised inside the `with` block**, not the disconnect cause.
v2.0.1's code re-raises "the exception on the disconnect future" — but that exception **was already raised elsewhere by `MessagesIterator.__anext__` via `raise MqttError(...) from ...`** (in `client.py`, `msg = "Disconnected during message iteration"`).
Both paths raise at the same time, so after `async for` normally receives `MqttError`, the `async with` exit **throws once more** (duplicate / masks the real stack).
2.5.0's change is **to eliminate this layer of duplicate raising**, letting the exception bubble up along the message-iteration path — so the CHANGELOG says "bubble up ... instead of suppressing", but **what was removed is the extra re-raise inside `__aexit__`**.

### 2.5 Actual impact on the I-1 claim

- **The upgrade direction is still correct**: 2.5.0 did change `__aexit__`, so the `aiomqtt>=2.5.0` requirement holds.
- But the causality "2.0.1 swallows the exception and prevents reconnect" is wrong: 2.0.1's `__aexit__` doesn't swallow, it over-raises. Reconnect relies on `MessagesIterator` raising `MqttError`, and **the reconnect branch is triggered under 2.0.1 too**.
- Therefore the comment in `backend_ingestion/app/services/mqtt_subscriber.py:49-51` ("2.0.1's __aexit__ has a branch that doesn't raise in the reconnect scenario, which bypasses this except") **does not match the source; it is recommended to change it to an accurate statement**.

---

## 3. Conclusion ②: the `Client` reusable statement —— ✅ **holds**

`external/aiomqtt`, `git show v2.5.1:docs/reconnection.md` verbatim original:

> The `Client` context is designed to be [reusable (but not reentrant)](https://docs.python.org/3/library/contextlib.html#reusable-context-managers).
>
> ```python
> client = aiomqtt.Client("test.mosquitto.org")
> interval = 5  # Seconds
> while True:
>     try:
>         async with client:
>             await client.subscribe("humidity/#")
>             async for message in client.messages:
>                 print(message.payload)
>     except aiomqtt.MqttError:
>         print(f"Connection lost; Reconnecting in {interval} seconds ...")
>         await asyncio.sleep(interval)
> ```

Source-side corroboration: `__aenter__` uses `self._lock` to block re-entry (`aiomqtt/client.py:769-771` raises `MqttReentrantError("The client context manager is reusable, but not reentrant")`).

This repo already implements this: `backend_ingestion/app/services/mqtt_subscriber.py:35-39` (construct outside the loop, `async with client` inside the loop) — **consistent, no further change needed**. Keeping exponential backoff (1→30s) instead of falling back to the official fixed 5s is a reasonable deviation for the 1000 msg/s scenario.

---

## 4. Conclusion ③: Alertmanager inhibition semantics —— ⚠️ **statement basically holds, but one point needs clarification**

`external/alertmanager`, `git show` to get `docs/configuration.md` §Inhibition (:505-529) verbatim:

> An inhibition rule mutes an alert (target) matching a set of matchers
> when an alert (source) exists that matches another set of matchers.
> Both target and source alerts must have the same label values
> for the label names in the `equal` list.
>
> To prevent an alert from inhibiting itself, an alert that matches _both_ the
> target and the source side of a rule cannot be inhibited by alerts for which
> the same is true (including itself).

**The semantically correct part**: inhibition = when a set of "source" alerts exists, silence another set of "target" alerts; the `equal` list requires source/target to have the same values on these labels (naturally fits our `robot_id` association).

**The part needing clarification**: the plan wrote "high severity inhibits low severity" — Alertmanager's `inhibit_rule` **has no severity concept**; it matches purely on **matcher (labels)**.
`severity` appears in the official docs only as an **example label** (e.g. `{alertname="Watchdog", severity=~"warning|critical"}` at `configuration.md:738`), not as a built-in hierarchy.
The so-called "high severity inhibits low severity" is **artificially expressed via matcher**, for example:

```yaml
inhibit_rules:
  - source_matchers: [severity = "critical"]   # source: critical
    target_matchers: [severity = "warning"]    # target: warning
    equal: [robot_id]                          # only inhibit on the same machine
```

**The conclusion for this repo is unchanged**: `alert_engine/app/deduplicator.py` indeed has no inhibition mechanism, and `offline` (severity 3) and `battery_low` (severity 2) may still fire two rows on the same machine — "suggest adding inhibition later" holds. But if implemented, it should align with the "matcher + equal" model rather than hardcoding severity comparison.

---

## 5. Summary table

| # | Conclusion | Verdict | Basis |
|---|---|---|---|
| ① | aiomqtt 2.5.0 fixed `__aexit__` | ✅ the fix exists, but ❌ **the original causality was reversed** | `git diff v2.0.1 v2.5.0`; `CHANGELOG.md:48`; `inspect.getsource` across three versions |
| ① | "2.0.1 swallowed the exception and prevented reconnect" | ❌ **does not hold** (2.0.1 over-raises instead) | `client.py:727-729` vs `:807` |
| ② | `Client` reusable (not reentrant) | ✅ holds | `v2.5.1:docs/reconnection.md`; `client.py:769-771` |
| ③ | Alertmanager has inhibition, we lack it | ✅ holds | `docs/configuration.md:505-529` |
| ③ | "high severity inhibits low severity" | ⚠️ concept needs clarification (no built-in severity) | official docs have no built-in severity semantics, only example labels |

## 6. Unverified / remaining boundaries

- ①'s **runtime end-to-end behavior** (whether it reconnects as expected after a real disconnect) needs a real broker; not done. This report only proves "the version difference in source and runtime `__aexit__` implementation", not "the reconnect chain runs through on real hardware".
- ③'s inhibition rule is **not implemented**, only a design comparison.
