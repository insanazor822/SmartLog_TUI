"""General-purpose formatting helpers.

These existed in v1 as ``utils.py`` but were never wired into the app, so v2
dropped them. Three of them (``calculate_percentile`` in particular) are
genuinely useful for log analysis and are part of the public API, so they are
restored here — this time actually exported, documented and tested.

Nothing in here depends on the rest of the package, so it is safe to import
from scripts and notebooks.
"""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

__all__ = [
    "calculate_percentile",
    "escape_html",
    "format_duration",
    "format_timestamp",
    "human_readable_size",
    "truncate_string",
]

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def format_timestamp(ts: float | int | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a Unix timestamp for display.

    Returns ``"-"`` for missing or out-of-range values instead of raising, so it
    is safe to call on data straight out of a log file.
    """
    if not ts or ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return "-"


def truncate_string(s: str | None, max_length: int = 50, suffix: str = "...") -> str:
    """Shorten ``s`` to ``max_length`` characters, keeping ``suffix`` visible."""
    if not s:
        return ""
    if len(s) <= max_length:
        return s
    if max_length <= len(suffix):
        return s[:max_length]
    return s[: max_length - len(suffix)] + suffix


def escape_html(text: Any) -> str:
    """Escape HTML special characters (``None`` becomes an empty string)."""
    return html.escape("" if text is None else str(text))


def human_readable_size(size_bytes: int | float) -> str:
    """Render a byte count with a binary unit suffix (``1536`` -> ``1.50 KB``)."""
    size = float(size_bytes)
    if size <= 0:
        return "0 B"
    for unit in _SIZE_UNITS:
        if size < 1024.0 or unit == _SIZE_UNITS[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} {_SIZE_UNITS[-1]}"


def calculate_percentile(values: Sequence[float] | None, percentile: float) -> float:
    """Linear-interpolation percentile, matching :func:`numpy.percentile`.

    ``percentile`` is clamped to ``0..100``. Returns ``0.0`` for empty input.

    >>> calculate_percentile([1, 2, 3, 4], 50)
    2.5
    >>> calculate_percentile([1, 2, 3, 4], 99.9)
    3.997
    """
    if not values:
        return 0.0

    p = max(0.0, min(100.0, float(percentile)))
    ordered = sorted(float(v) for v in values)
    n = len(ordered)
    if n == 1:
        return ordered[0]

    # Same interpolation as numpy: k = (n - 1) * p / 100
    k = (n - 1) * (p / 100.0)
    low = math.floor(k)
    high = math.ceil(k)
    if low == high:
        return ordered[int(k)]
    d0 = ordered[low] * (high - k)
    d1 = ordered[high] * (k - low)
    return float(d0 + d1)


def format_duration(seconds: float | int | None) -> str:
    """Render a duration compactly (``0.045`` -> ``45ms``)."""
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"