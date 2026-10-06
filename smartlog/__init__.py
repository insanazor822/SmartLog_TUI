"""SmartLog: a fast terminal log analyser with cascading diagnostics.

Public API::

    from smartlog import LogStream, LogStats, FilterEngine, DiagnosticEngine

    stream = LogStream(["/var/log/syslog"])
    await stream.start()
    async for batch in stream.batches():
        ...
"""

from __future__ import annotations

__version__ = "2.0.0"

from .diagnostics import (
    Confidence,
    Diagnosis,
    DiagnosticEngine,
    Recommendation,
    discover_providers,
)
from .exporters import Clipboard, Exporter, ExportMeta, default_filename, read_log_file
from .filters import FilterEngine, FilterSpec, build_spec
from .knowledge import KnowledgeBase, Rule, match_rules
from .models import LEVEL_COLORS, Level, LogEntry
from .parser import FINGERPRINTS, Fingerprint, LineParser, parse_entry
from .reader import FileState, LogStream, LogStreamManager, TailWorker
from .stats import LogStats, StatsSnapshot, render_sparkline
from .utils import (
    calculate_percentile,
    escape_html,
    format_duration,
    format_timestamp,
    human_readable_size,
    truncate_string,
)

__all__ = [
    "FINGERPRINTS",
    "LEVEL_COLORS",
    "Clipboard",
    "Confidence",
    "Diagnosis",
    "DiagnosticEngine",
    "ExportMeta",
    "Exporter",
    "FileState",
    "FilterEngine",
    "FilterSpec",
    "Fingerprint",
    "KnowledgeBase",
    "Level",
    "LineParser",
    "LogEntry",
    "LogStats",
    "LogStream",
    "LogStreamManager",
    "Recommendation",
    "Rule",
    "StatsSnapshot",
    "TailWorker",
    "__version__",
    "build_spec",
    "calculate_percentile",
    "default_filename",
    "discover_providers",
    "escape_html",
    "format_duration",
    "format_timestamp",
    "human_readable_size",
    "match_rules",
    "parse_entry",
    "read_log_file",
    "render_sparkline",
    "truncate_string",
]