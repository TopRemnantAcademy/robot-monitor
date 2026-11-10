"""Offline-detection rule (spec/02 §3.1) -- **must be driven by an independent ticker**.

Strictly forbidden to rely on Redis Key expiration events:
- Redis's keyspace notification (`notify-keyspace-events`) is **off by default**;
- and after TTL expiry `robot:state:{id}` is already deleted, so the engine cannot read last_seen.

Hence main.py's ticker iterates all 100 robots every second.

Criterion source (ISSUE #96)
---------------------
**Prefer reading `robot_metadata.is_online`**, no longer infer it in this process with an in-memory dict.
That column is batch-maintained by the backend `backend_ingestion/app/services/metadata_writer.py`
heartbeat: every telemetry sets `last_seen=server time / is_online=TRUE`, and no telemetry for
`state_ttl_seconds` has the same module periodically set FALSE. It is the single source of truth for
"the server's last sighting of this robot"; if this engine re-derives it, it creates a second truth (two
processes' clocks, restart windows, and Redis jitter each produce a different judgment -> the dashboard
says online while the alert says offline).

**Migration-period fallback**: `is_online` may be None -- the column has not yet been written (early in
deployment no one writes the heartbeat yet, or this engine cannot read that row). In that case **fall
back to the last_seen time-delta judgment** rather than judging offline directly; otherwise "column not
yet written" would be false-alarmed as "100 machines all offline". After fallback it still uses the
`offline_after_s` threshold, semantically **byte-identical** to the pre-#96 version.
"""

from __future__ import annotations

import time
from collections import deque

from app.config import settings
from app.rules.base import Alert, Rule


class OfflineRule(Rule):
    rule_id = "offline"
    severity = 3                                  # critical
    dedup_window_s = settings.offline_dedup_s

    def __init__(self) -> None:
        self.after_s = settings.offline_after_s
        self.use_is_online = settings.offline_use_is_online

    # ---- Reuse the same alert construction to avoid text/field drift between the two paths ----
    def _alert(self, rid: str, last_seen: float | None) -> Alert:
        if last_seen is None:
            return Alert(
                robot_id=rid,
                rule_id=self.rule_id,
                severity=self.severity,
                message="Robot offline: never reported telemetry",
                detail={"age_s": None},
            )
        age = time.time() - last_seen
        return Alert(
            robot_id=rid,
            rule_id=self.rule_id,
            severity=self.severity,
            message=f"Robot offline: no telemetry for {age:.1f}s (threshold {self.after_s}s)",
            detail={"age_s": round(age, 1), "threshold_s": self.after_s},
        )

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        """state is constructed by ticker: {"robot_id", "is_online": bool|None, "last_seen": epoch|None}.

        Judgment priority (when `offline_use_is_online=True`):
          1. `is_online is True`  -> do not report (backend explicitly says it is online)
          2. `is_online is False` -> report (backend explicitly says it is offline) -- even if in-memory
                                     last_seen is very recent, the column is authoritative (this is the very
                                     evidence of "single-source criterion")
          3. `is_online is None`  -> column unavailable, **fall back** to the last_seen time-delta judgment
        """
        rid = state.get("robot_id", "")
        last_seen = state.get("last_seen")
        is_online = state.get("is_online")

        if self.use_is_online and is_online is not None:
            if is_online:
                return None
            # Backend judged offline: the message carries last_seen so a human can tell "never reported"
            # from "went offline after reporting"; the judgment itself does not depend on it.
            return self._alert(rid, last_seen)

        # Fallback path: column unavailable (None) or switch off -> use in-memory last_seen time delta.
        if last_seen is None:
            return self._alert(rid, None)
        age = time.time() - last_seen
        if age <= self.after_s:
            return None
        return self._alert(rid, last_seen)
