"""Tests for the restored formatting helpers."""

from __future__ import annotations

import pytest

from smartlog.utils import (
    calculate_percentile,
    escape_html,
    format_duration,
    format_timestamp,
    human_readable_size,
    truncate_string,
)


class TestFormatTimestamp:
    def test_formats_epoch(self):
        assert format_timestamp(0) == "-"          # 0 is treated as missing
        assert format_timestamp(None) == "-"
        assert format_timestamp(-5) == "-"
        assert format_timestamp(1_700_000_000) != "-"

    def test_custom_format(self):
        text = format_timestamp(1_700_000_000, fmt="%Y")
        assert text == "2023"

    @pytest.mark.parametrize("bad", [1e30, -1e30])
    def test_out_of_range_does_not_raise(self, bad):
        assert format_timestamp(bad) == "-"


class TestTruncateString:
    def test_short_string_unchanged(self):
        assert truncate_string("abc") == "abc"

    def test_truncates_with_suffix(self):
        result = truncate_string("abcdefghij", max_length=8)
        assert result == "abcde..."
        assert len(result) == 8

    def test_none_and_empty(self):
        assert truncate_string(None) == ""
        assert truncate_string("") == ""

    def test_max_length_shorter_than_suffix(self):
        assert truncate_string("abcdef", max_length=2) == "ab"

    def test_exact_length_unchanged(self):
        assert truncate_string("abcde", max_length=5) == "abcde"


class TestEscapeHtml:
    @pytest.mark.parametrize("raw,escaped", [
        ("<script>", "&lt;script&gt;"),
        ("a & b", "a &amp; b"),
        ('say "hi"', "say &quot;hi&quot;"),
    ])
    def test_escapes(self, raw, escaped):
        assert escape_html(raw) == escaped

    def test_none(self):
        assert escape_html(None) == ""


class TestHumanReadableSize:
    @pytest.mark.parametrize("size,expected", [
        (0, "0 B"),
        (-10, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KB"),
        (1536, "1.50 KB"),
        (1024 ** 2, "1.00 MB"),
        (1024 ** 3, "1.00 GB"),
        (1024 ** 5, "1.00 PB"),
        (1024 ** 6, "1.00 EB"),
        # Beyond the largest unit the value keeps counting in EB.
        (1024 ** 8, "1048576.00 EB"),
    ])
    def test_sizes(self, size, expected):
        assert human_readable_size(size) == expected


class TestCalculatePercentile:
    def test_matches_numpy_semantics(self):
        values = [1, 2, 3, 4]
        assert calculate_percentile(values, 0) == 1.0
        assert calculate_percentile(values, 50) == 2.5
        assert calculate_percentile(values, 100) == 4.0
        assert calculate_percentile(values, 99.9) == pytest.approx(3.997)

    def test_single_value(self):
        assert calculate_percentile([7], 50) == 7.0

    def test_empty(self):
        assert calculate_percentile([], 50) == 0.0
        assert calculate_percentile(None, 50) == 0.0

    def test_unsorted_input(self):
        assert calculate_percentile([4, 1, 3, 2], 50) == 2.5

    def test_percentile_is_clamped(self):
        assert calculate_percentile([1, 2, 3], -50) == 1.0
        assert calculate_percentile([1, 2, 3], 500) == 3.0

    def test_typical_latency_distribution(self):
        # The realistic use case: what is the p99 request latency?
        latencies = [10, 12, 15, 20, 25, 30, 40, 50, 80, 400]
        p99 = calculate_percentile(latencies, 99)
        assert 350 < p99 <= 400
        assert calculate_percentile(latencies, 50) < p99


class TestFormatDuration:
    @pytest.mark.parametrize("seconds,expected", [
        (None, "-"),
        (-1, "-"),
        (0.045, "45ms"),
        (1.5, "1.50s"),
        (90, "1m 30s"),
        (3700, "1h 1m"),
        (90000, "1d 1h"),
    ])
    def test_durations(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestPublicApi:
    @pytest.mark.parametrize("name", [
        "format_timestamp", "truncate_string", "escape_html",
        "human_readable_size", "calculate_percentile", "format_duration",
    ])
    def test_exported_from_package_root(self, name):
        import smartlog

        assert name in smartlog.__all__
        assert getattr(smartlog, name) is not None