"""Statistical anomaly-detection rule (competitive benchmark B: edge ML).

The existing rules (`motor_temp` / `battery_low` / `joint_stuck`) are all *static
thresholds* that only catch values crossing a fixed red line. They miss two
cases:

  1. A robot whose own baseline is already high/low (e.g. a joint that idles at
     60C) -- the fixed 75C red line is too loose for it.
  2. Slow drift + sudden jump (e.g. battery falling from 40% to 12% in one
     minute) -- no single sample crosses the line, but the trend is abnormal.

This rule computes a **per-robot rolling z-score** over the consumer's existing
sliding window (`window`, up to 100 frames ~= 10s of history) to catch
"relative to this robot's own baseline" anomalies.

Design (consistent with the rest of the engine)
----------------------------------------------
- No new state: the window already carries history, so the rule is stateless
  (like the other rules).
- No ML dependency: Netdata-style 18-models-per-metric is too heavy for a
  resource-constrained box; z-score is the same-effect, zero-dependency
  approximation (called out explicitly in the gap analysis).
- Only alert on metrics with **enough variance**: `std < anomaly_min_std` is
  treated as normal noise, avoiding false positives on steady robots (floating
  point jitter).

It reuses the `Rule` interface (`evaluate(state, window) -> Alert | None`), driven
by `app.main`'s `RULES`, so the consume path is unchanged. It returns the single
most anomalous metric (max |z|) to avoid duplicate alerts from one spike across
multiple joints.
"""

from __future__ import annotations

import statistics
from collections import deque

from app.config import settings
from app.rules.base import Alert, Rule

# Scalar metric keys (field names in the state dict).
SCALAR_METRICS = ("battery_pct", "max_joint_temp")


def _series(window: deque, key: str) -> list[float]:
    """Extract the valid scalar series for `key` from the window."""
    out: list[float] = []
    for w in window:
        v = w.get(key)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _joint_series(window: deque, joint: str) -> list[float]:
    """Extract the per-joint temperature series (joint_temperatures[joint])."""
    out: list[float] = []
    for w in window:
        temps = w.get("joint_temperatures")
        if isinstance(temps, dict):
            v = temps.get(joint)
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out


def _zscore(current: float, series: list[float]) -> float | None:
    """Return the z-score of `current` relative to `series`; None if the sample
    is too small or the variance too small to be meaningful."""
    if len(series) < settings.anomaly_min_samples or len(series) < 2:
        return None
    mean = statistics.fmean(series)
    try:
        std = statistics.stdev(series)
    except statistics.StatisticsError:
        return None
    if std < settings.anomaly_min_std:
        return None
    return (current - mean) / std


class AnomalyRule(Rule):
    rule_id = "anomaly"
    severity = 2                          # warning (one tier below motor_temp's critical)
    dedup_window_s = settings.anomaly_dedup_s

    def __init__(self) -> None:
        self.zscore = settings.anomaly_zscore
        self.min_std = settings.anomaly_min_std
        self.min_samples = settings.anomaly_min_samples

    def evaluate(self, state: dict, window: deque) -> Alert | None:
        rid = state.get("robot_id", "")
        if len(window) < self.min_samples:
            return None

        best: tuple[float, str, float, float] | None = None  # (|z|, metric, value, std)

        # Per-joint temperatures (latest frame's known joint set).
        # NOTE: evaluated *before* the scalar metrics. `max_joint_temp` is the
        # maximum joint temperature (a derived column); when a joint spikes the
        # two have equal z, and reporting the *specific joint* is more
        # diagnosable. The strict `>` comparison ensures a scalar metric only
        # overrides the joint conclusion when it is *strictly* more anomalous.
        temps = state.get("joint_temperatures")
        if isinstance(temps, dict):
            for joint, cur in temps.items():
                if not isinstance(cur, (int, float)):
                    continue
                z = _zscore(float(cur), _joint_series(window, joint))
                if z is not None and abs(z) >= self.zscore:
                    if best is None or abs(z) > best[0]:
                        mean = statistics.fmean(_joint_series(window, joint))
                        std = statistics.stdev(_joint_series(window, joint))
                        best = (abs(z), f"joint_temperatures.{joint}", float(cur), std)

        # Scalar metrics (battery_pct / max_joint_temp).
        for metric in SCALAR_METRICS:
            cur = state.get(metric)
            if not isinstance(cur, (int, float)):
                continue
            z = _zscore(float(cur), _series(window, metric))
            if z is not None and abs(z) >= self.zscore:
                if best is None or abs(z) > best[0]:
                    mean = statistics.fmean(_series(window, metric))
                    std = statistics.stdev(_series(window, metric))
                    best = (abs(z), metric, float(cur), std)

        if best is None:
            return None

        _absz, metric, value, std = best
        # Recompute mean/z from the true series so the message and detail are exact.
        if metric.startswith("joint_temperatures."):
            j = metric.split(".", 1)[1]
            series = _joint_series(window, j)
        else:
            series = _series(window, metric)
        mean = statistics.fmean(series)
        z = (value - mean) / std

        return Alert(
            robot_id=rid,
            rule_id=self.rule_id,
            severity=self.severity,
            message=(
                f"statistical anomaly: {metric}={value:.1f} "
                f"(mean={mean:.1f}, std={std:.2f}, z={z:+.1f})"
            ),
            detail={
                "metric": metric,
                "value": value,
                "mean": round(mean, 2),
                "std": round(std, 2),
                "zscore": round(z, 2),
            },
        )
