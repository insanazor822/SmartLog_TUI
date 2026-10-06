"""Tests for statistics, sliding-window rates and anomaly detection."""

from __future__ import annotations

import time

import pytest

from smartlog.models import Level, LogEntry
from smartlog.stats import LogStats, _SlidingWindow, render_sparkline


def entry(level: Level = Level.INFO, *, source: str = "app.log",
          fingerprint: str = "", ts: float = 0.0, raw: str = "x") -> LogEntry:
    return LogEntry(raw=raw, source=source, level=level, fingerprint=fingerprint,
                    ts_epoch=ts)


class TestSlidingWindow:
    def test_add_and_total(self):
        w = _SlidingWindow(span_seconds=60)
        now = int(time.time())
        for _ in range(5):
            w.add(now)
        assert w.total() == 5

    def test_expired_buckets_are_evicted(self):
        w = _SlidingWindow(span_seconds=10)
        w.add(1000)
        w.add(1001)
        w.trim(1020)
        assert w.total() == 0
        assert not w._buckets      # memory is actually reclaimed

    def test_rate_normalises_by_covered_span(self):
        w = _SlidingWindow(span_seconds=60)
        now = int(time.time())
        for _ in range(30):
            w.add(now)
        # One second of data only -> 30/s, not 0.5/s.
        assert w.rate_per_second(now) == pytest.approx(30.0, rel=0.01)

    def test_series_has_fixed_width(self):
        w = _SlidingWindow(span_seconds=60)
        now = int(time.time())
        w.add(now - 3)
        w.add(now)
        series = w.series(now, buckets=10)
        assert len(series) == 10
        assert series[-1] == 1
        assert series[0] == 0

    def test_reset_clears(self):
        w = _SlidingWindow(10)
        w.add(int(time.time()))
        w.reset()
        assert w.total() == 0
        assert not w._buckets


class TestLogStats:
    def test_counts_levels_and_sources(self):
        stats = LogStats()
        stats.record(entry(Level.INFO))
        stats.record(entry(Level.ERROR, fingerprint="db_deadlock"))
        stats.record(entry(Level.ERROR, source="nginx.log", fingerprint="http_5xx"))
        snap = stats.snapshot()
        assert snap.total_lines == 3
        assert snap.total_errors == 2
        assert snap.level_counts["ERROR"] == 2
        assert snap.source_counts["nginx.log"] == 1

    def test_top_fingerprints_ranked(self):
        stats = LogStats()
        for _ in range(5):
            stats.record(entry(Level.ERROR, fingerprint="timeout"))
        for _ in range(2):
            stats.record(entry(Level.ERROR, fingerprint="oom"))
        snap = stats.snapshot()
        assert snap.top_fingerprints[0] == ("timeout", 5)
        assert dict(snap.top_fingerprints)["oom"] == 2

    def test_error_ratio(self):
        stats = LogStats()
        stats.record(entry(Level.INFO))
        stats.record(entry(Level.ERROR))
        assert stats.snapshot().error_ratio == pytest.approx(0.5)

    def test_rate_is_rolling_not_cumulative(self):
        """The original reported total lines / total elapsed, which is not a rate."""
        stats = LogStats()
        for _ in range(10):
            stats.record(entry())
        lines_per_sec, _ = stats.rates()
        # 10 lines in the current second is ~10/s, not "10 / uptime".
        assert lines_per_sec >= 1.0

    def test_reset(self):
        stats = LogStats()
        stats.record(entry(Level.ERROR, fingerprint="oom"))
        stats.reset()
        snap = stats.snapshot()
        assert snap.total_lines == 0
        assert snap.total_errors == 0
        assert snap.top_fingerprints == []

    def test_drop_accounting(self):
        stats = LogStats()
        stats.drop(42)
        assert stats.snapshot().dropped_lines == 42

    def test_rate_query_is_cheap(self):
        """The O(n)-per-line bug: rates must not scale with history length."""
        stats = LogStats()
        now = time.time()
        for i in range(50_000):
            stats.record(entry(ts=now - i * 0.001))
        start = time.perf_counter()
        for _ in range(1000):
            stats.rates()
        elapsed = time.perf_counter() - start
        # 1000 queries must be far cheaper than 1000 x 50000 element scans.
        assert elapsed < 0.5, f"1000 rate queries took {elapsed:.3f}s"

    def test_sparkline_rendered(self):
        stats = LogStats()
        for _ in range(30):
            stats.record(entry())
        assert stats.snapshot().sparkline != ""


class TestSparkline:
    def test_empty(self):
        assert render_sparkline([]) == ""

    def test_flat_series(self):
        assert render_sparkline([5, 5, 5]) == "▃▃▃"

    def test_increasing_series(self):
        spark = render_sparkline([0, 1, 2, 3, 4, 5, 6, 7])
        assert len(spark) == 8
        assert spark[-1] == "█"

    def test_width_is_capped(self):
        assert len(render_sparkline(list(range(100)), width=20)) == 20


class TestAnomalyDetection:
    def test_spike_requires_volume_and_ratio(self):
        stats = LogStats()
        stats._baseline = 1.0
        # A single error must never be flagged as a spike.
        stats.record(entry(Level.ERROR, fingerprint="timeout"))
        assert stats.snapshot().spike_active is False

    def test_sustained_error_burst_sets_spike(self):
        stats = LogStats()
        stats._baseline = 0.5
        now = time.time()
        for _ in range(60):
            stats.record(entry(Level.ERROR, fingerprint="http_5xx", ts=now))
        snap = stats.snapshot()
        assert snap.errors_last_minute >= 60
        assert snap.spike_ratio > 3.0

    def test_quiet_system_never_spikes(self):
        stats = LogStats()
        now = time.time()
        for _ in range(5_000):
            stats.record(entry(ts=now))
        assert stats.snapshot().spike_active is False