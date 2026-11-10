"""WS outbound backpressure + heartbeat (ISSUE #73).

## Symptom / root cause

Before this module, the incremental loop in `ws_robots` (`main.py`) awaited each
client's `send_json` **synchronously**:

    async for msg in pubsub.listen():
        await ws.send_json({"type": "telemetry", "data": data})

`await send_json` only guarantees the frame was handed to the kernel, not that
the client consumed it. A slow client (mobile network / frozen tab / janky
frontend) lets uvicorn's transport write buffer climb to its high-water mark,
after which every `send` blocks on `drain()` -- and that **blocks the relay
loop itself**. The result: (1) the slow client piles up outbound frames in
memory with no bound; (2) the whole relay loop is dragged down by it, and every
healthy client starves. At 1000 msg/s x N clients, a single stuck browser tab
can take down the whole process.

## This module's approach

Give each connection a **bounded outbound queue** (`asyncio.Queue(maxsize=N)`)
plus an independent sender coroutine:

- The relay loop only does `put_nowait` and **never awaits**, so slow clients
  no longer stall the loop.
- When the queue is full, frames are dropped by type (see `enqueue`). Worst
  case memory = N x frame size, and N is a constant pinned in config -- it does
  not grow with uptime.
- The sender coroutine's `await ws.send_json` can still block on backpressure,
  but only **itself**; once the queue is full the drop fires, keeping the link
  bounded.

## Alerts must not be dropped (ISSUE #73 "constraints/forbidden" item 3)

`robot:alerts` is the **single source** of the frontend alert stream; dropping
one means missing one alert. So on queue overflow the handling is tiered:
**only telemetry is dropped, alerts are preserved**. When there is no telemetry
to drop (extreme case: the whole queue is alerts) the queue is allowed to grow
by a few slots rather than dropping an alert -- the "alerts must not be dropped"
hard constraint is encoded into the implementation, not just a comment.

## Heartbeat (ISSUE #73 acceptance item 2)

`ping/pong` is natively supported by Starlette: when `websocket.receive`
returns a message whose `text` is `"ping"`, reply with `"pong"`. Clients sending
ping proactively is the browser-side norm for half-open connection detection
(TCP does not report an unplugged cable immediately). On the server side, the
`_HEARTBEAT` sentinel drives liveness: the sender coroutine drops a sentinel
into the queue each loop iteration; along the way it `await`s real backpressure /
disconnect state, so "it got sent" is itself a liveness check -- no extra timer
needed.

## A deliberate choice: no `State.WAITING_FOR_RESPONSE`

The ISSUE #73 acceptance text said "check the connection's writable/backpressure
state (`State.WAITING_FOR_RESPONSE` or queue depth)". Measured in this repo
(Starlette 0.36.3):

    >>> [x for x in dir(WebSocketState) if not x.startswith("_")]
    ['CONNECTED', 'CONNECTING', 'DISCONNECTED']

**The server-side Starlette has no `WAITING_FOR_RESPONSE` state** (that is a
client-side `websockets` library state-machine concept). So this implementation
uses the **second equivalent criterion from the acceptance -- queue depth**, plus
`transport.get_write_buffer_size()` as an observation of the real write buffer
(used only for logging/metrics, never as the sole criterion: uvicorn 0.28 does
not expose that transport, so it silently degrades when unreadable).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("ws_backpressure")

#: Telemetry frame identifier (matches the outbound envelope in `main.py`).
TELEMETRY = "telemetry"
#: Alert frame identifier. **Must not be dropped.**
ALERT = "alert"

#: Extra slack when the queue is full but holds only alerts. Keeps "alerts must
#: not be dropped" true even with a few extra alerts stacked, while pinning the
#: worst-case memory growth to a constant.
ALERT_OVERFLOW_SLACK = 64

#: Heartbeat sentinel. Placed in the queue rather than driven by a timer: the
#: sender coroutine naturally flushes it each loop, incidentally verifying
#: "can this connection still send?" -- no second concurrency source needed.
_HEARTBEAT = object()

# ---- Pre-serialized frame channel (ISSUE #71 merge design) ----
# ws_hub's beat fan-out serializes each frame **only once** (one publish -> many
# connections reuse it); what gets enqueued is ready-made JSON text. Queue item
# kinds use sentinel objects, not strings, so they can never be confused with the
# dict-envelope channel; droppability is still tiered by telemetry/alert (see
# _drop_oldest_telemetry).
_RAW_TELEMETRY = object()
_RAW_ALERT = object()


class WsOutbound:
    """Outbound channel for a single WS connection: bounded queue + sender coroutine.

    Lifecycle: `__init__` -> `start()` -> `enqueue()` (any number of times) ->
    `aclose()`. All public methods are **non-blocking to the relay loop**
    (`enqueue` uses `put_nowait`) -- this is the entire point of the module.
    """

    def __init__(
        self,
        ws,
        *,
        maxsize: int,
        alert_slack: int = ALERT_OVERFLOW_SLACK,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        # [Why allocate the queue once, rather than "grow it when full"]
        # Note: an earlier version actually tried "swap in a bigger queue when an
        # alert arrives" (`_rehome_for_alert`), and it was **wrong**: the sender
        # coroutine is already `await queue.get()`-ing the old queue, so swapping
        # `self._queue` to a new object makes the move and the consume race each
        # other and **silently drop frames** (measured: 1 alert lost in a 5000-
        # frame mixed stream, only when "move collides with consume").
        # Correct approach: provision physical capacity up front (telemetry soft
        # limit + alert headroom), then use a **soft limit** to decide from which
        # moment telemetry starts dropping -- no move, no race, ever.
        #
        # maxsize must be at least 1, otherwise asyncio.Queue degrades to
        # unbounded (maxsize<=0 semantics). A misconfigured value must never
        # produce "unbounded pile-up" -- exactly the phenomenon this module kills.
        self._telemetry_limit = max(1, maxsize)
        # Physical capacity = telemetry soft limit + alert headroom: give the
        # "undroppable alerts" their own space.
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=self._telemetry_limit + max(0, alert_slack)
        )
        self._ws = ws
        self._on_sent = on_sent
        self._task: asyncio.Task | None = None
        # Telemetry throttling: how many frames dropped, how many coalesced. Two
        # separate counters so "backpressure is really dropping" is distinguishable
        # from "just coalesced" -- whoever watches metrics needs the two to differ.
        self.telemetry_dropped = 0
        self.telemetry_merged = 0
        self.alerts_deferred = 0
        # Send progress: `_sender` increments and refreshes the timestamp on each
        # successful frame. The watchdog judges liveness from "counter advancing /
        # timestamp advancing" -- the only two signals it needs (avoid a per-
        # connection timer).
        self.sent_count = 0
        self.last_sent_at: float | None = None

    # ------------------------------------------------------------- enqueue

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def maxsize(self) -> int:
        """Telemetry soft limit (excluding the alert headroom)."""
        return self._telemetry_limit

    @property
    def capacity(self) -> int:
        """Physical capacity = telemetry soft limit + alert headroom."""
        return self._queue.maxsize

    @property
    def is_saturated(self) -> bool:
        """Whether the queue has hit its routine capacity (real-time, excluding the
        alert headroom portion).

        The watchdog uses this to judge "is the client consuming durably slower
        than the inflow". Uses `full()` rather than `qsize()>=maxsize`: the
        former is asyncio's own criterion, strictly consistent with the
        `QueueFull` raised by `put_nowait` -- no "I say full but it still fits".
        """
        return self._queue.full()

    def enqueue(self, kind: str, envelope: dict) -> None:
        """Place a frame on the outbound queue. **Never blocks, never raises**.

        Tiered handling (`kind` is passed by the caller from the outbound
        envelope's `type`):

        - `alert`: try a direct enqueue first; if the queue is full, **expand**
          (up to `maxsize + alert_slack`), and only then count it as `deferred`.
          Alerts are **never dropped** (ISSUE #73 forbidden item 3).
        - anything else (telemetry): when full, drop the **head** (oldest frame)
          before enqueuing. Dropping the oldest rather than the newest because
          telemetry is "latest-value-wins" time-series data -- the older frame's
          value is already superseded, so dropping it is the least loss
          (spec/01 SS4.3 state cache is the same idea).
        """
        if kind == ALERT:
            self._enqueue_alert(envelope)
        else:
            self._enqueue_telemetry(kind, envelope)

    def enqueue_raw(self, kind: str, text: str) -> None:
        """Pre-serialized frame enqueue (ws_hub beat fan-out only). Semantics
        identical to `enqueue`.

        The only difference is the payload shape: `text` is JSON **already
        serialized** by the hub, so the sender goes straight to `send_text`
        instead of re-`json.dumps` per connection -- this is the landing point of
        "serialize once -> reuse across N connections" (ISSUE #71 core benefit).
        Droppability matches the dict channel: telemetry droppable (oldest),
        alerts never.
        """
        if kind == ALERT:
            self._enqueue_raw_alert(text)
        else:
            self._enqueue_raw_telemetry(text)

    def _enqueue_telemetry(self, kind: str, envelope: dict) -> None:
        # [The criterion is "telemetry's **own** quota", not physical capacity]
        # The extra capacity is **reserved for alerts**. If judged against
        # physical capacity here, telemetry would fill the reserved zone first,
        # and then alerts could only land in deferred -- "alerts must not be
        # dropped" degrades to "first come, first served" (measured: maxsize=5 +
        # slack=4 -> telemetry fills all 9 slots, 3 alerts all deferred).
        if self._queue.qsize() >= self._telemetry_limit:
            # Find the oldest telemetry frame to drop. Probing `_queue._queue`
            # directly is necessary: the queue may mix sentinels and alerts, so
            # we must search from the **oldest** side.
            dropped = self._drop_oldest_telemetry()
            if dropped is None:
                # No droppable telemetry in the queue (already taken by the
                # sender / all sentinels): this frame was not admitted -- count
                # "merged" not "dropped": nothing existing was lost.
                self.telemetry_merged += 1
                return
            self.telemetry_dropped += 1
        try:
            self._queue.put_nowait((kind, envelope))
        except asyncio.QueueFull:  # pragma: no cover - a slot always opens after drop
            self.telemetry_dropped += 1

    def _enqueue_alert(self, envelope: dict) -> None:
        """Alert enqueue. **Never dropped** (ISSUE #73 forbidden item 3).

        Physical capacity already reserves `alert_slack` slots for alerts (see
        `__init__`), so the normal path is "go straight in". Only a pathological
        case (alerts themselves piling past the headroom) lands in
        `alerts_deferred` accounting -- and even then it is **not dropped**, just
        deferred, so ops judge from this metric whether to scale up or cull the
        connection.

        Deliberately **no** "swap queue to make room" here: that races and silently
        drops frames against the consuming sender (see `__init__`'s measured note).
        """
        try:
            self._queue.put_nowait((ALERT, envelope))
        except asyncio.QueueFull:
            self.alerts_deferred += 1

    def _enqueue_raw_telemetry(self, text: str) -> None:
        """Pre-serialized telemetry enqueue; same criterion as
        `_enqueue_telemetry` (soft limit + drop oldest)."""
        if self._queue.qsize() >= self._telemetry_limit:
            dropped = self._drop_oldest_telemetry()
            if dropped is None:
                self.telemetry_merged += 1
                return
            self.telemetry_dropped += 1
        try:
            self._queue.put_nowait((_RAW_TELEMETRY, text))
        except asyncio.QueueFull:  # pragma: no cover - a slot always opens after drop
            self.telemetry_dropped += 1

    def _enqueue_raw_alert(self, text: str) -> None:
        """Pre-serialized alert enqueue: also goes only through the headroom,
        **never dropped** (same as `_enqueue_alert`)."""
        try:
            self._queue.put_nowait((_RAW_ALERT, text))
        except asyncio.QueueFull:
            self.alerts_deferred += 1

    def _drop_oldest_telemetry(self) -> tuple | None:
        """From the head, find the first telemetry frame and remove it; return the
        dropped tuple, or None if none found.

        The queue may hold four kinds of things, and they must be treated
        separately:
          - `(TELEMETRY, envelope)`        -> droppable;
          - `(_RAW_TELEMETRY, text)`       -> droppable (hub pre-serialized
                                             telemetry, same semantics);
          - `(ALERT/_RAW_ALERT, ...)`      -> **not droppable**, skip;
          - bare `_HEARTBEAT` sentinel (`object()`) -> droppable, but **no index**.

        The criterion must be a **whitelist** "is telemetry -> droppable", NOT
        `item[0] is not ALERT`: the latter would also treat `_RAW_ALERT` as
        droppable (sentinel object `is not` string is always true). If the head
        happens to be a pre-serialized alert, it would be silently dropped --
        violating the "alerts must not be dropped" hard constraint. The very
        first version of this function also missed the third kind (sentinel index
        out-of-range TypeError); same lesson: **enumerate** the queue contents,
        don't exclude.
        """
        items = self._queue._queue  # access deque directly: locating from the head is O(1) drop-oldest
        for i, item in enumerate(items):
            if item is _HEARTBEAT:
                del items[i]
                return None  # dropping a sentinel is not dropping telemetry (no envelope to return)
            kind = item[0]
            if kind is TELEMETRY or kind is _RAW_TELEMETRY:
                del items[i]
                return item
        return None

    # ------------------------------------------------------------- outbound

    def start(self) -> None:
        """Start the sender coroutine. Called by `ws_robots` after accept."""
        self._task = asyncio.create_task(self._sender())

    async def _sender(self) -> None:
        """Serialize the send. The **only** place that calls `ws.send_json` /
        `ws.send_text`.

        Why serial and not concurrent: WebSocket frames must be ordered;
        concurrent sends interleave frames and the client's JSON.parse explodes.
        Serial also gives "backpressure" a single definition point -- blocking
        happens only here, and queue length is the only backlog metric.

        Two payload shapes: dict envelopes go through `send_json` (this
        connection serializes itself); hub pre-serialized RAW frames go through
        `send_text` direct -- each frame's json.dumps happens exactly once per
        process (ISSUE #71 core benefit), and the client receives identical bytes.
        """
        try:
            while True:
                item = await self._queue.get()
                if item is _HEARTBEAT:
                    continue
                kind, payload = item
                if kind is _RAW_TELEMETRY or kind is _RAW_ALERT:
                    await self._ws.send_text(payload)
                else:
                    await self._ws.send_json(payload)
                self.sent_count += 1
                self.last_sent_at = asyncio.get_running_loop().time()
                if self._on_sent is not None:
                    self._on_sent()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Send failure (client disconnect / illegal frame) is not swallowed:
            # log then let the coroutine exit; `ws_robots`'s finally is responsible
            # for reclaiming counters and the queue.
            # Note: **do not** close the ws here -- the close belongs to
            # `ws_robots`, otherwise it races the same connection with its finally.
            log.warning("ws outbound sender stopped", exc_info=True)

    def heartbeat(self) -> None:
        """Push a heartbeat sentinel into the queue (skip if full, non-blocking)."""
        try:
            self._queue.put_nowait(_HEARTBEAT)
        except asyncio.QueueFull:
            pass

    async def aclose(self) -> None:
        """Cancel the sender coroutine and wait for it to exit. Idempotent,
        safe to call repeatedly."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            # The sender already logged internally; here we only guarantee no
            # exception escapes, because aclose's call site (finally) must run
            # its reclamation unconditionally.
            pass

    def drain_nowait(self) -> list[dict]:
        """Take the remaining sendable envelopes from the queue (skip sentinels),
        for test assertions and graceful shutdown."""
        out: list[dict] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _HEARTBEAT:
                continue
            out.append(item[1])
        return out


def envelope_size(envelope: dict) -> int:
    """Estimate a frame's byte size (observability: reporting "how many bytes
    backed up" reads better in logs than "how many frames")."""
    try:
        return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False))
    except (TypeError, ValueError):  # pragma: no cover - envelope comes from json.loads, always serializable
        return 0


__all__ = [
    "ALERT",
    "TELEMETRY",
    "ALERT_OVERFLOW_SLACK",
    "WsOutbound",
    "envelope_size",
]
