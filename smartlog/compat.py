"""v1 compatibility layer.

SmartLog v2 rewrote the internals, which renamed and restructured most of v1's
public surface. This module re-exposes the v1 names so existing integrations
keep working instead of breaking on upgrade.

Everything here is a thin adapter over the v2 API - no v2 code depends on this
module, so it can be deleted once downstream users have migrated.

Mapping
-------
===========================  ==========================================
v1                          v2
===========================  ==========================================
``LogLevel``                ``Level`` (alias)
``LogEntry``                same
``SystemClipboard``         ``Clipboard``
``ErrorKnowledgeBase``      ``KnowledgeBase``
``AIDiagnosticEngine``      ``DiagnosticEngine``
``RecommendedFix``          ``Recommendation``
``Diagnosis``               ``Diagnosis``
``LogStreamReader``         ``LogStream``
``LogStreamManager``        ``LogStreamManager``
``LogParser.analyze_line``  ``LineParser.parse``
``LogParser.parse_level``   ``Level.from_any``
``LogParser.detect_errors`` ``LineParser.classify``
``LogParser.refresh_rates`` ``LogStats.snapshot``
``LogExporter``             ``Exporter``
``LogExporter.export_to_file``    ``Exporter.write``
``LogExporter.copy_to_clipboard`` ``Exporter.copy``
``get_buffer()``            ``LogStream.snapshot()``
``get_last_n_entries(n)``   ``LogStream.tail(n)``
===========================  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diagnostics import Diagnosis, DiagnosticEngine, Recommendation
from .exporters import Clipboard, Exporter, read_log_file
from .filters import FilterEngine
from .knowledge import KnowledgeBase
from .models import Level, LogEntry
from .models import Level as LogLevel
from .parser import LineParser
from .reader import LogStream, LogStreamManager, TailWorker
from .stats import LogStats, StatsSnapshot

# v1 spelling of Recommendation
RecommendedFix = Recommendation

__all__ = [
    "AIDiagnosticEngine",
    "Diagnosis",
    "ErrorKnowledgeBase",
    "LogEntry",
    "LogExporter",
    "LogLevel",
    "LogParser",
    "LogStreamManager",
    "LogStreamReader",
    "RecommendedFix",
    "SystemClipboard",
    "to_v2_entry",
]


class SystemClipboard:
    """v1 name for :class:`smartlog.exporters.Clipboard`."""

    @staticmethod
    def copy(text: str) -> tuple[bool, str]:
        return Clipboard.copy(text)


class ErrorKnowledgeBase:
    """v1 name for :class:`smartlog.knowledge.KnowledgeBase`."""

    def __init__(self) -> None:
        self._inner = KnowledgeBase()

    def find_matching_diagnosis(
        self, error_message: str, context: str | None = None
    ) -> tuple[str, dict[str, Any]] | None:
        """Return ``(pattern, payload)`` for the best matching rule."""
        text = f"{error_message} {context or ''}".strip()
        hit = self._inner.best(text)
        if hit is None:
            return None
        rule, matched = hit
        return rule.id, {
            "summary": rule.title,
            "root_cause": rule.root_cause,
            "confidence": rule.confidence,
            "recommendations": [
                Recommendation(title=title, description=desc, command=command or None)
                for title, desc, command in rule.checks
            ],
            "prevention_tips": list(rule.prevention),
            "matched": matched,
        }


AIDiagnosticEngine = DiagnosticEngine


class LogParser:
    """v1-shaped facade over :class:`smartlog.parser.LineParser`.

    Only the methods v1 actually exposed are kept. ``analyze_line`` returns a
    dict with v1's key names so existing ``result['is_error']`` style code works.
    """

    def __init__(self) -> None:
        self._parser = LineParser()
        self._stats = LogStats()

    def parse_level(self, text: str) -> LogLevel:
        return Level.from_any(text)

    def parse(self, line: str, source: str = "", line_number: int = 0) -> LogEntry:
        return self._parser.parse(line, source, line_number)

    def analyze_line(self, line: str) -> dict[str, Any]:
        """v1-compatible analysis result."""
        entry = self._parser.parse(line)
        level, _fingerprint, _duration = self._parser.classify(entry.message)
        entry.level = max(entry.level, level)
        return {
            "level": entry.level,
            "level_priority": int(entry.level),
            "is_error": entry.level >= Level.ERROR,
            "errors": [(entry.fingerprint, entry.fingerprint)] if entry.fingerprint else [],
            "max_severity": int(entry.level),
            "error_categories": [entry.fingerprint] if entry.fingerprint else [],
            "entry": entry,
        }

    def detect_errors(self, message: str) -> list[tuple[str, str]]:
        _, fingerprint, _ = self._parser.classify(message)
        return [(fingerprint, fingerprint)] if fingerprint else []

    def update_statistics(self, line: str, source: str = "") -> None:
        self._stats.record(self._parser.parse(line, source))

    def refresh_rates(self, current_time: float | None = None) -> None:
        self._stats.snapshot()

    def get_statistics_summary(self) -> dict[str, Any]:
        snapshot = self._stats.snapshot()
        return {
            "total_lines": snapshot.total_lines,
            "lines_per_second": snapshot.lines_per_sec,
            "levels": snapshot.level_counts,
            "sources": snapshot.source_counts,
            "total_errors": snapshot.total_errors,
            "errors_by_level": snapshot.error_counts,
            "errors_by_category": snapshot.category_counts,
            "errors_last_minute": snapshot.errors_last_minute,
            "error_rate_per_minute": snapshot.error_rate_per_min,
            "is_spike": snapshot.spike_active,
            "unique_errors": len(snapshot.top_fingerprints),
        }

    def get_top_errors(self, n: int = 10) -> list[tuple[str, int]]:
        return self._stats.snapshot().top_fingerprints[:n]

    def get_errors_by_category(self) -> dict[str, int]:
        return self._stats.snapshot().category_counts

    def reset_statistics(self) -> None:
        self._stats.reset()


class LogStreamReader:
    """v1 name for :class:`smartlog.reader.LogStream`.

    v1 defaulted to ``start_at_end=False`` semantics only when the file already
    existed at construction time; v2's ``LogStream`` tails from EOF unless
    told otherwise, so the flag is passed straight through.
    """

    def __init__(self, log_paths: list[str], buffer_size: int = 1000,
                 encoding: str = "utf-8",
                 start_at_end: bool = False) -> None:
        self._stream = LogStream(log_paths, buffer_size=buffer_size,
                                 encoding=encoding,
                                 start_at_end=start_at_end)

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        await self._stream.start()

    async def stop(self) -> None:
        await self._stream.stop()

    def add_log_path(self, path: str) -> None:
        self._stream.add_path(path)

    def remove_log_path(self, path: str) -> None:
        self._stream.remove_path(path)

    def pause(self) -> None:
        self._stream.pause()

    def resume(self) -> None:
        self._stream.resume()

    def is_paused(self) -> bool:
        return self._stream.paused

    # -- consumption ------------------------------------------------------ #

    def get_buffer(self) -> list[LogEntry]:
        return self._stream.snapshot()

    def clear_buffer(self) -> None:
        self._stream.clear()

    def get_last_n_entries(self, n: int) -> list[LogEntry]:
        return self._stream.tail(n)


class LogExporter:
    """v1 name for :class:`smartlog.exporters.Exporter`."""

    def __init__(self, source_paths: list[str] | None = None) -> None:
        self.source_paths = list(source_paths or [])
        self._inner = Exporter()

    @staticmethod
    def normalize_format(fmt: str) -> str:
        from .exporters import normalise_format

        return normalise_format(fmt)

    @staticmethod
    def default_filename(fmt: str) -> str:
        from .exporters import default_filename

        return default_filename(fmt)

    def export_to_file(self, entries: list[LogEntry], fmt: str, path: Any,
                       **kwargs: Any) -> tuple[bool, str]:
        try:
            written = self._inner.write(entries, fmt, path,
                                        sources=self.source_paths, **kwargs)
        except (OSError, ValueError) as exc:
            return False, str(exc)
        return True, str(written)

    def copy_to_clipboard(self, entries: list[LogEntry], fmt: str,
                          **kwargs: Any) -> tuple[bool, str]:
        return self._inner.copy(entries, fmt, sources=self.source_paths, **kwargs)


def to_v2_entry(item: Any) -> LogEntry:
    """Coerce a v1-ish object (or dict) into a v2 :class:`LogEntry`."""
    if isinstance(item, LogEntry):
        return item
    if isinstance(item, dict):
        raw = str(item.get("raw") or item.get("message") or item.get("text") or "")
        return LogEntry(
            raw=raw,
            source=str(item.get("source") or item.get("file") or ""),
            level=Level.from_any(item.get("level") or item.get("severity")),
            timestamp=str(item.get("timestamp") or item.get("ts") or ""),
            message=str(item.get("message") or raw),
            line_number=int(item.get("line_number") or 0),
            fingerprint=str(item.get("fingerprint") or ""),
        )
    return LogEntry(raw=str(item), source="", message=str(item))


@dataclass(slots=True)
class _CompatStats:
    """Placeholder kept so ``from smartlog.compat import *`` stays explicit."""

    snapshot: StatsSnapshot | None = None
    engine: FilterEngine | None = None
    worker: TailWorker | None = None
    read: Any = read_log_file