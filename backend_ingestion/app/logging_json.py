"""Structured JSON logging (order 16 T4).

Why not continue using `logging.basicConfig(format="%(asctime)s %(levelname)s ...")`:
order 07's collection stack (openobserve / loki) reads line by line; only a
**single-line JSON** can be directly parsed by `jq` / log pipelines into fields.
The current space-separated text, when message contains spaces (e.g.
"batch flush gave up, 500 rows dropped"), cannot be reliably split; the collection
side can only do full-text regex, and a field rename silently mismatches.

Contract (order 16 SS1 T4): one JSON object per line, at least containing
    ts        ISO8601 UTC timestamp
    level     log level (INFO / WARNING / ...)
    logger    logger name
    msg       formatted human-readable message
    robot_id  optional: present only when the LogRecord carries a robot_id attribute

Implementation points:
- Inherit `logging.Formatter`; `format()` returns a **newline-free** single-line
  string. logging itself appends the newline; writing one extra would produce a
  blank line (some collectors misread a blank line as end-of-log).
- `default=str` fallback: a LogRecord may carry non-JSON-native types (exception
  objects, datetime); without this json.dumps throws TypeError, and logging
  **swallows** formatter exceptions internally (prints to stderr instead of
  crashing the process), resulting in "the whole log line disappears" with no sign.
- Exception stack goes into the `exc` field (not stuffed into msg): keeps it
  structured while not breaking single-line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

# LogRecord built-in fields NOT put into JSON -- they are logging implementation
# details; exporting all of them would add 15+ useless keys per line (collection-
# side storage cost doubles directly).
_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord into single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            # `record.created` is epoch seconds (float); convert to UTC ISO8601 for
            # cross-timezone debugging.
            # Do not use datetime.now(): that is the **formatting moment**, which
            # under concurrency misaligns with the event moment.
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            # getMessage() merges %s params; using record.msg would drop the params
            # and leave "batch flush %d".
            "msg": record.getMessage(),
        }

        # Custom fields attached by the caller via `log.info("...", extra={"robot_id": rid})`.
        # Only include non-built-in ones, and let their values be JSON-able
        # (backstopped by default=str).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value

        if record.exc_info:
            # formatException returns a **multi-line** string. json.dumps by default
            # escapes it to \n, so single-line is fine; but if this module is ever
            # concatenated by something other than json.dumps (or someone swaps the
            # serializer), it degrades to multi-line. Here we explicitly normalize
            # newlines so the "single-line" contract does not depend on the
            # downstream serializer (order 16 SS2 "single-line JSON").
            payload["exc"] = self.formatException(record.exc_info).replace("\n", " | ")

        # ensure_ascii=False: Chinese messages stay readable (collection side does
        # not need to decode again).
        # default=str: backstop non-native types, avoiding the whole log line
        # vanishing from serialization failure.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Switch the root logger to single-line JSON output.

    Idempotent: repeated calls only replace the handler, never stack (stacking
    would print the same line N times).
    Must also clear existing handlers -- uvicorn installs its own by default;
    without clearing, the container log would **mix** JSON lines with uvicorn's
    text lines, and the collection side would fail to parse half of them.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
