"""Incremental metrics, sliding-window rates and anomaly detection.

Performance notes
-----------------
The previous implementation kept a deque of *every* error timestamp and ran a
linear ``sum()`` over the last five minutes **on every incoming line** to compute
the one-minute rate. At a few thousand lines/sec that alone saturates a core.

Here rates are maintained with **per-second buckets**:

* insertion is O(1) (``dict[int, int]``),
* eviction of expired buckets is amortised O(1) (ordered ``deque[int]``),
* a rate query is O(window seconds) and is only executed on the UI tick (1 Hz),
  never per line.

Throughput is a true rolling average over the last ``--window`` seconds instead
of "total lines since start", which is what the old code reported.
"""

from __future__ import annotations

import math
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import LogEntry

__all__ = ["SPARK_CHARS", "LogStats", "StatsSnapshot"]

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


@dataclass(slots=True)
class StatsSnapshot:
    """Immutable view handed to the UI / exporters."""

    total_lines: int = 0
    dropped_lines: int = 0
    lines_per_sec: float = 0.0
    peak_lines_per_sec: float = 0.0
    uptime_sec: float = 0.0
    level_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    error_counts: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, int] = field(default_factory=dict)
    top_fingerprints: list[tuple[str, int]] = field(default_factory=list)
    total_errors: int = 0
    errors_last_minute: int = 0
    errors_last_5m: int = 0
    error_rate_per_min: float = 0.0
    baseline_rate: float = 0.0
    spike_active: bool = False
    spike_ratio: float = 0.0
    sparkline: str = ""
    last_updated: float = field(default_factory=time.time)

    @property
    def error_ratio(self) -> float:
        if self.total_lines <= 0:
            return 0.0
        return self.total_errors / self.total_lines


class _SlidingWindow:
    """Fixed-resolution sliding window over per-second counters."""

    __slots__ = ("_buckets", "_order", "_span", "_total")

    def __init__(self, span_seconds: int) -> None:
        self._span = span_seconds
        self._buckets: dict[int, int] = {}
        self._order: deque[int] = deque()
        self._total = 0

    def add(self, second: int, count: int = 1) -> None:
        bucket = self._buckets.get(second)
        if bucket is None:
            self._buckets[second] = count
            self._order.append(second)
        else:
            self._buckets[second] = bucket + count
        self._total += count

    def trim(self, newest_second: int) -> None:
        cutoff = newest_second - self._span
        order = self._order
        buckets = self._buckets
        while order and order[0] <= cutoff:
            stale = order.popleft()
            self._total -= buckets.pop(stale, 0)

    def total(self) -> int:
        return self._total

    def span_seconds(self, now_second: int) -> float:
        """Window length actually covered, for rate normalisation."""
        oldest = self._order[0] if self._order else now_second
        return max(1.0, float(min(now_second - oldest + 1, self._span)))

    def rate_per_second(self, now_second: int) -> float:
        return self._total / self.span_seconds(now_second)

    def series(self, now_second: int, buckets: int) -> list[int]:
        """Return a fixed-length count series ending at ``now_second``."""
        self.trim(now_second)
        base = now_second - buckets + 1
        getter = self._buckets.get
        return [getter(base + i, 0) for i in range(buckets)]

    def reset(self) -> None:
        self._buckets.clear()
        self._order.clear()
        self._total = 0


class LogStats:
    """Thread-confined incremental statistics accumulator.

    The reader thread owns an instance; the UI only ever reads snapshots.
    """

    __slots__ = (
        "_baseline",
        "_categories",
        "_dropped",
        "_error_levels",
        "_error_window",
        "_fingerprints",
        "_last_epoch_ts",
        "_last_second",
        "_last_tick",
        "_levels",
        "_minute_window",
        "_rate_window",
        "_sources",
        "_spark",
        "_spark_buckets",
        "_spike_active",
        "_spike_ratio",
        "_started",
        "_top_n",
        "_total_errors",
        "_total_lines",
    )

    def __init__(self, spark_buckets: int = 60, top_n: int = 12) -> None:
        self._started = time.monotonic()
        self._last_tick = self._started
        self._last_second = 0
        self._total_lines = 0
        self._total_errors = 0
        self._dropped = 0

        self._rate_window = _SlidingWindow(60)
        self._error_window = _SlidingWindow(300)
        self._minute_window = _SlidingWindow(60)

        self._levels: Counter[str] = Counter()
        self._sources: Counter[str] = Counter()
        self._error_levels: Counter[str] = Counter()
        self._categories: Counter[str] = Counter()
        self._fingerprints: Counter[str] = Counter()
        self._top_n = top_n

        # EWMA baseline of the error rate (per minute) used for spike detection.
        self._baseline = 0.5
        self._spike_active = False
        self._spike_ratio = 0.0

        self._spark_buckets = spark_buckets
        self._spark: list[int] = []
        self._last_epoch_ts = 0.0

    # -- ingestion -------------------------------------------------------- #

    def record(self, entry: LogEntry) -> None:
        """Fold a single entry into the metrics. O(1) amortised."""
        self._total_lines += 1
        self._levels[entry.level.label] += 1
        if entry.source:
            self._sources[entry.source] += 1

        # Prefer the timestamp carried by the log; fall back to arrival time.
        stamp = entry.ts_epoch if entry.ts_epoch > 0 else time.time()
        self._last_epoch_ts = max(self._last_epoch_ts, stamp)
        second = int(stamp)

        self._rate_window.add(second)
        self._rate_window.trim(second)
        self._error_window.trim(second)
        self._minute_window.trim(second)

        if entry.is_error:
            self._total_errors += 1
            self._error_levels[entry.level.label] += 1
            self._error_window.add(second)
            self._minute_window.add(second)
            if entry.fingerprint:
                self._categories[entry.fingerprint] += 1
                self._fingerprints[entry.fingerprint] += 1
        else:
            self._error_window.trim(second)

    def record_many(self, entries: list[LogEntry]) -> None:
        for entry in entries:
            self.record(entry)

    def drop(self, count: int = 1) -> None:
        """Account for lines shed by backpressure so totals stay honest."""
        self._dropped += count

    # -- queries ---------------------------------------------------------- #

    @property
    def total_lines(self) -> int:
        return self._total_lines

    @property
    def total_errors(self) -> int:
        return self._total_errors

    def rates(self) -> tuple[float, float]:
        """Return ``(lines_per_sec, errors_per_sec)`` over the rolling window."""
        now_second = self._now_second()
        return (self._rate_window.rate_per_second(now_second),
                self._error_window.rate_per_second(now_second))

    def errors_per_minute(self) -> float:
        return self._minute_window.rate_per_second(self._now_second()) * 60.0

    def snapshot(self, *, detect_spike: bool = True) -> StatsSnapshot:
        """Build an immutable snapshot; call this on the UI tick, not per line."""
        now_second = self._now_second()
        self._rate_window.trim(now_second)
        self._error_window.trim(now_second)
        self._minute_window.trim(now_second)

        lps = self._rate_window.rate_per_second(now_second)
        err_per_min = self.errors_per_minute()

        # ---- spike detection ------------------------------------------- #
        # Compare the current 1-minute error rate against a slowly moving
        # baseline, with an absolute floor so a quiet system cannot spike.
        ratio = (err_per_min / self._baseline) if self._baseline > 0 else 0.0
        significant = err_per_min >= 5.0 and self._total_errors >= 10
        self._spike_active = significant and ratio >= SPIKE_RATIO
        self._spike_ratio = ratio

        now = time.monotonic()
        if now - self._last_tick >= BASELINE_INTERVAL:
            alpha = 1.0 - math.exp(-(now - self._last_tick) / BASELINE_TAU)
            observed = max(err_per_min, 1.0)
            self._baseline = (1 - alpha) * self._baseline + alpha * observed
            self._last_tick = now

        # ---- sparkline -------------------------------------------------- #
        series = self._rate_window.series(now_second, self._spark_buckets)
        self._spark = series

        # ---- peak throughput (per-second high-water mark) ---------------- #
        peak = max(series, default=0)

        top = self._fingerprints.most_common(self._top_n)

        return StatsSnapshot(
            total_lines=self._total_lines,
            dropped_lines=self._dropped,
            lines_per_sec=round(lps, 2),
            peak_lines_per_sec=float(peak),
            uptime_sec=round(time.monotonic() - self._started, 1),
            level_counts=dict(self._levels),
            source_counts=dict(self._sources.most_common(10)),
            error_counts=dict(self._error_levels),
            category_counts=dict(self._categories.most_common(10)),
            top_fingerprints=top,
            total_errors=self._total_errors,
            errors_last_minute=self._minute_window.total(),
            errors_last_5m=self._error_window.total(),
            error_rate_per_min=round(err_per_min, 2),
            baseline_rate=round(self._baseline, 2),
            spike_active=self._spike_active,
            spike_ratio=round(ratio, 2),
            sparkline=self.sparkline(),
            last_updated=time.time(),
        )

    def sparkline(self) -> str:
        """Unicode sparkline of the per-second line rate."""
        if not self._spark:
            return ""
        return render_sparkline(self._spark)

    def _now_second(self) -> int:
        return int(time.time())

    # -- lifecycle -------------------------------------------------------- #

    def reset(self) -> None:
        """Reset counters without rebuilding the object (keeps hot references)."""
        self._started = time.monotonic()
        self._last_tick = self._started
        self._total_lines = 0
        self._total_errors = 0
        self._dropped = 0
        self._rate_window.reset()
        self._error_window.reset()
        self._minute_window.reset()
        self._levels.clear()
        self._sources.clear()
        self._error_levels.clear()
        self._categories.clear()
        self._fingerprints.clear()
        self._baseline = 0.5
        self._spike_active = False
        self._spike_ratio = 0.0
        self._spark.clear()
        self._last_epoch_ts = 0.0


SPIKE_RATIO = 3.0
BASELINE_INTERVAL = 5.0
BASELINE_TAU = 30.0


def render_sparkline(data: Sequence[int | float], width: int = 60) -> str:
    """Render values as a Unicode sparkline, scaling to the observed range."""
    if not data:
        return ""
    values = data[-width:]
    lo = min(values)
    hi = max(values)
    if hi == lo:
        # A flat series gets a mid block rather than an empty one, so "steady
        # traffic" is visually distinct from "no traffic".
        return SPARK_CHARS[0] * len(values) if hi <= 0 else SPARK_CHARS[3] * len(values)
    span = float(hi - lo)
    last = len(SPARK_CHARS) - 1
    return "".join(
        SPARK_CHARS[max(0, min(last, int((v - lo) / span * last)))] for v in values
    )