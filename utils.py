"""
Utility functions for SmartLog TUI.
Provides log exporters (HTML/JSON), data formatting, and terminal visualization helpers.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def format_timestamp(ts: float | int | None) -> str:
    """Format Unix timestamp to human-readable string safely."""
    if not ts or ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return "-"


def truncate_string(s: str, max_length: int = 50, suffix: str = "...") -> str:
    """Truncate a string to maximum length safely."""
    if not s:
        return ""
    if len(s) <= max_length:
        return s
    return s[: max(0, max_length - len(suffix))] + suffix


def escape_html(text: Any) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text) if text is not None else "")


def _normalize_log_entry(entry: Any) -> dict[str, Any]:
    """Convert LogEntry dataclass or arbitrary object into a uniform dictionary with string values."""
    if is_dataclass(entry) and not isinstance(entry, type):
        data = asdict(entry)
    elif isinstance(entry, dict):
        data = dict(entry)
    else:
        data = {
            "timestamp": getattr(entry, "timestamp", ""),
            "level": getattr(entry, "level", "INFO"),
            "message": getattr(entry, "message", str(entry)),
            "raw": getattr(entry, "raw", str(entry)),
            "source": getattr(entry, "source", ""),
        }

    # Ensure level is always a clean uppercase string (Enum safe)
    level = data.get("level", "INFO")
    data["level"] = str(getattr(level, "value", level or "INFO")).upper()
    return data


def export_logs_to_json(
    logs: list[Any],
    output_path: str | Path,
    stats: dict[str, Any] | None = None,
) -> Path:
    """
    Export logs to a formatted JSON file.
    Supports both LogEntry dataclass instances and standard dictionaries.
    """
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    normalized_logs = [_normalize_log_entry(log) for log in logs]

    data = {
        "exported_at": datetime.now().isoformat(),
        "total_entries": len(normalized_logs),
        "statistics": stats or {},
        "logs": normalized_logs,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    return path


def export_logs_to_html(
    logs: list[Any],
    output_path: str | Path,
    stats: dict[str, Any] | None = None,
) -> Path:
    """
    Export logs to a standalone, searchable HTML report.
    Optimized for high throughput using fast buffer joining.
    """
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    normalized_logs = [_normalize_log_entry(log) for log in logs]

    # Modern color theme for log levels
    level_colors = {
        "DEBUG": "#6c757d",
        "INFO": "#28a745",
        "WARN": "#ffc107",
        "WARNING": "#ffc107",
        "ERROR": "#dc3545",
        "CRITICAL": "#e83e8c",
        "FATAL": "#721c24",
    }

    total_errors = (
        stats.get("total_errors", 0)
        if stats and "total_errors" in stats
        else sum(
            1
            for l in normalized_logs
            if l.get("level", "") in ["ERROR", "CRITICAL", "FATAL"]
        )
    )

    parts: list[str] = [
        f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SmartLog Export Report</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Consolas', monospace;
            background: #0f172a;
            color: #f8fafc;
            margin: 0;
            padding: 24px;
        }}
        .header {{
            background: #1e293b;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 24px;
            border: 1px solid #334155;
        }}
        .stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin-top: 12px; }}
        .stat {{
            background: #334155;
            padding: 8px 16px;
            border-radius: 6px;
            font-weight: 500;
        }}
        .stat span {{ font-weight: bold; color: #38bdf8; }}
        .log-container {{ display: flex; flex-direction: column; gap: 4px; }}
        .log-entry {{
            padding: 8px 12px;
            background: #1e293b;
            border-radius: 4px;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 13px;
            line-height: 1.5;
            border-left: 4px solid #64748b;
        }}
        .log-entry.error {{ background: rgba(220, 53, 69, 0.15); border-left-color: #dc3545; }}
        .log-entry.warn {{ background: rgba(255, 193, 7, 0.1); border-left-color: #ffc107; }}
        .timestamp {{ color: #94a3b8; margin-right: 12px; font-size: 12px; }}
        .source {{ color: #a78bfa; margin-right: 10px; font-weight: bold; }}
        .level {{ font-weight: bold; margin-right: 12px; display: inline-block; width: 75px; }}
        .message {{ word-break: break-all; color: #f1f5f9; }}
        .search-box {{ margin-bottom: 20px; }}
        .search-box input {{
            width: 100%; max-width: 450px; padding: 10px 16px;
            background: #1e293b; border: 1px solid #475569;
            color: #fff; border-radius: 6px; font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1 style="margin:0 0 8px 0; font-size: 24px;">📋 SmartLog Analysis Report</h1>
        <p style="margin:0; color: #94a3b8;">Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <div class="stats">
            <div class="stat">Total Lines: <span>{len(normalized_logs):,}</span></div>
            <div class="stat">Errors: <span style="color:#f87171">{total_errors:,}</span></div>
"""
    ]

    if stats and "lines_per_second" in stats:
        parts.append(
            f"""            <div class="stat">Throughput: <span>{stats['lines_per_second']:.1f} lines/sec</span></div>\n"""
        )

    parts.append(
        """        </div>
    </div>

    <div class="search-box">
        <input type="text" id="search" placeholder="🔍 Search logs in real-time..." onkeyup="filterLogs()">
    </div>

    <div id="logs" class="log-container">
"""
    )

    for log in normalized_logs:
        level = log.get("level", "INFO")
        level_color = level_colors.get(level, "#cbd5e1")
        is_error = "error" if level in ["ERROR", "CRITICAL", "FATAL"] else ""
        is_warn = "warn" if level in ["WARN", "WARNING"] else ""

        timestamp = escape_html(log.get("timestamp", ""))
        source = escape_html(log.get("source", ""))
        message = escape_html(log.get("message") or log.get("raw") or str(log))

        source_badge = f'<span class="source">[{source}]</span>' if source else ""

        parts.append(
            f"""        <div class="log-entry {is_error} {is_warn}">
            <span class="timestamp">{timestamp}</span>
            <span class="level" style="color: {level_color}">[{level:7}]</span>
            {source_badge}
            <span class="message">{message}</span>
        </div>\n"""
        )

    parts.append(
        """    </div>

    <script>
        function filterLogs() {
            const query = document.getElementById('search').value.toLowerCase().trim();
            const entries = document.querySelectorAll('.log-entry');
            for (let i = 0; i < entries.length; i++) {
                const entry = entries[i];
                if (!query || entry.textContent.toLowerCase().includes(query)) {
                    entry.style.display = '';
                } else {
                    entry.style.display = 'none';
                }
            }
        }
    </script>
</body>
</html>
"""
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))

    return path


def generate_sparkline(data: list[int | float], width: int = 40) -> str:
    """Generate a text-based Unicode sparkline chart for the most recent data points."""
    if not data:
        return "─" * width

    recent_data = data[-width:] if len(data) > width else data
    min_val = min(recent_data)
    max_val = max(recent_data)
    range_val = max_val - min_val

    # Standard Unicode 8-level block characters (\u2581 through \u2588)
    chars = " ▂▃▄▅▆▇█"

    result: list[str] = []
    for value in recent_data:
        if range_val == 0:
            result.append(chars[3] if min_val > 0 else chars[0])
        else:
            normalized = (value - min_val) / range_val
            char_idx = int(normalized * (len(chars) - 1))
            char_idx = max(0, min(len(chars) - 1, char_idx))
            result.append(chars[char_idx])

    chart = "".join(result)
    if len(chart) < width:
        chart = (" " * (width - len(chart))) + chart

    return chart


def calculate_percentile(values: list[float] | list[int], percentile: float) -> float:
    """
    Calculate the statistical percentile (0 to 100) of values using linear interpolation.
    Matches the standard behavior of numpy.percentile.
    """
    if not values:
        return 0.0

    p = max(0.0, min(100.0, float(percentile)))
    sorted_values = sorted(float(v) for v in values)
    n = len(sorted_values)

    if n == 1:
        return sorted_values[0]

    # Linear interpolation formula
    k = (n - 1) * (p / 100.0)
    floor_idx = math.floor(k)
    ceil_idx = math.ceil(k)

    if floor_idx == ceil_idx:
        return sorted_values[int(k)]

    d0 = sorted_values[floor_idx] * (ceil_idx - k)
    d1 = sorted_values[ceil_idx] * (k - floor_idx)
    return float(d0 + d1)


def human_readable_size(size_bytes: int | float) -> str:
    """Convert byte count to a clean human-readable string."""
    size = float(size_bytes)
    if size <= 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0

    return f"{size:.2f} PB"
