"""
Log Parser and Error Detection Module for SmartLog TUI
Handles log level detection, error pattern matching, and statistics.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    WARNING = "WARN"  # Alias mapped to WARN
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    FATAL = "FATAL"

    @property
    def priority(self) -> int:
        priorities = {
            'DEBUG': 0,
            'INFO': 1,
            'WARN': 2,
            'ERROR': 3,
            'CRITICAL': 4,
            'FATAL': 5,
        }
        return priorities.get(self.value, 1)

    @property
    def is_error(self) -> bool:
        return self.priority >= 3


@dataclass
class ErrorPattern:
    """Defines a pattern for error detection."""

    name: str
    pattern: re.Pattern[str]
    severity: int = 3
    category: str = "General"


@dataclass
class AnomalyStats:
    """Statistics for anomaly detection."""

    total_errors: int = 0
    errors_by_level: dict[str, int] = field(default_factory=dict)
    errors_by_source: dict[str, int] = field(default_factory=dict)
    errors_by_category: dict[str, int] = field(default_factory=dict)
    error_timestamps: deque[float] = field(default_factory=deque)  # Efficient O(1) sliding window
    unique_error_messages: dict[str, int] = field(default_factory=dict)

    # Rate tracking
    errors_last_minute: int = 0
    errors_last_5_minutes: int = 0
    error_rate_per_minute: float = 0.0

    # Spike detection
    is_spike: bool = False
    spike_threshold: float = 3.0  # 3x normal baseline rate


@dataclass
class LogStatistics:
    """Comprehensive log statistics."""

    total_lines: int = 0
    lines_per_second: float = 0.0
    levels: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    first_timestamp: float = 0.0
    last_timestamp: float = 0.0

    # Rolling window stats
    recent_lines: deque[tuple[float, str]] = field(
        default_factory=lambda: deque(maxlen=1000)
    )


class LogParser:
    """
    Advanced log parser with error detection and statistics tracking.
    Supports standard plaintext logs and structured JSON logs.
    """

    # Accurate word-boundary level matching compiled pattern
    LEVEL_PATTERN = re.compile(
        r'\b(CRITICAL|FATAL|ERROR|WARNING|WARN|DEBUG|INFO)\b',
        re.IGNORECASE,
    )

    # Default error patterns with refined contextual regexes
    DEFAULT_PATTERNS = [
        ErrorPattern(
            name="HTTP 5xx Error",
            pattern=re.compile(
                r'(?:HTTP/\d(?:\.\d)?|status|code|response)?[:\s"\'\[]*\b(5\d{2})\b',
                re.IGNORECASE,
            ),
            severity=4,
            category="HTTP",
        ),
        ErrorPattern(
            name="HTTP 4xx Error",
            pattern=re.compile(
                r'(?:HTTP/\d(?:\.\d)?|status|code|response)?[:\s"\'\[]*\b(4\d{2})\b',
                re.IGNORECASE,
            ),
            severity=2,
            category="HTTP",
        ),
        ErrorPattern(
            name="Python Exception",
            pattern=re.compile(
                r'\b(Traceback|(?:\w+Error|\w+Exception)|Exception)\b(?::\s*(\S+))?',
                re.IGNORECASE,
            ),
            severity=4,
            category="Exception",
        ),
        ErrorPattern(
            name="Connection Error",
            pattern=re.compile(
                r'\b(connection\s+(?:refused|reset|timeout|closed)|ECONNREFUSED|ECONNRESET)\b',
                re.IGNORECASE,
            ),
            severity=3,
            category="Network",
        ),
        ErrorPattern(
            name="Memory Error",
            pattern=re.compile(
                r'\b(out of memory|memory allocation failed|OOM(?:Killed)?|MemoryError)\b',
                re.IGNORECASE,
            ),
            severity=5,
            category="Resource",
        ),
        ErrorPattern(
            name="Disk Error",
            pattern=re.compile(
                r'\b(no space left on device|disk full|ENOSPC|IOError)\b',
                re.IGNORECASE,
            ),
            severity=5,
            category="Resource",
        ),
        ErrorPattern(
            name="Permission Denied",
            pattern=re.compile(
                r'\b(permission denied|access denied|EACCES|Forbidden|Unauthorized)\b',
                re.IGNORECASE,
            ),
            severity=3,
            category="Permission",
        ),
        ErrorPattern(
            name="Authentication Failure",
            pattern=re.compile(
                r'\b(authentication failed|invalid (?:credentials|token|password)|auth failed)\b',
                re.IGNORECASE,
            ),
            severity=3,
            category="Security",
        ),
        ErrorPattern(
            name="Database Error",
            pattern=re.compile(
                r'\b(database error|sql (?:syntax )?error|query failed|deadlock detected|connection pool exhausted)\b',
                re.IGNORECASE,
            ),
            severity=4,
            category="Database",
        ),
        ErrorPattern(
            name="Timeout",
            pattern=re.compile(
                r'\b(timed? ?out|ETIMEDOUT|RequestTimeout)\b',
                re.IGNORECASE,
            ),
            severity=3,
            category="Network",
        ),
    ]

    def __init__(self, custom_patterns: list[ErrorPattern] | None = None) -> None:
        self.patterns = self.DEFAULT_PATTERNS.copy()
        if custom_patterns:
            self.patterns.extend(custom_patterns)

        self.stats = LogStatistics()
        self.anomaly_stats = AnomalyStats()
        self._last_rate_update = time.time()
        self._baseline_error_rate = 1.0  # Baseline errors per minute

    def _extract_level_from_str(self, text: str) -> LogLevel:
        """Extract LogLevel from a string using regex matching."""
        match = self.LEVEL_PATTERN.search(text)
        if match:
            lvl_str = match.group(1).upper()
            if lvl_str == "WARNING":
                lvl_str = "WARN"
            if lvl_str in LogLevel.__members__:
                return LogLevel[lvl_str]
        return LogLevel.INFO

    def parse_level(self, text: str) -> LogLevel:
        """
        Accurately parse log level from text.
        Supports structured JSON strings and standard plaintext.
        """
        stripped = text.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    for key in ('level', 'severity', 'log_level', 'lvl', 'logLevel'):
                        if key in data and data[key]:
                            return self._extract_level_from_str(str(data[key]))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        return self._extract_level_from_str(text)

    def detect_errors(self, message: str) -> list[tuple[ErrorPattern, str]]:
        """
        Detect errors in a message using configured patterns.
        Returns list of (pattern, matched_text) tuples.
        """
        detected: list[tuple[ErrorPattern, str]] = []
        for pattern in self.patterns:
            for match in pattern.pattern.finditer(message):
                matched_text = match.group(0).strip()
                detected.append((pattern, matched_text))
        return detected

    def analyze_line(self, line: str) -> dict[str, Any]:
        """
        Perform comprehensive analysis on a log line.
        Supports JSON structured logs as well as plaintext logs.
        """
        stripped = line.strip()
        target_message = line
        level: LogLevel | None = None

        # JSON parsing attempt
        if stripped.startswith('{') and stripped.endswith('}'):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    # 1. Extract level from JSON keys
                    for key in ('level', 'severity', 'log_level', 'lvl', 'logLevel'):
                        if key in data and data[key]:
                            level = self._extract_level_from_str(str(data[key]))
                            break

                    # 2. Extract message from JSON keys
                    for key in ('message', 'msg', 'error', 'err', 'text', 'description'):
                        if key in data and data[key]:
                            target_message = str(data[key])
                            break
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Fallback to plain text analysis if JSON wasn't present or level wasn't found
        if level is None:
            level = self.parse_level(line)

        # Run error pattern matching
        errors = self.detect_errors(target_message)
        if not errors and target_message != line:
            errors = self.detect_errors(line)

        has_error_pattern = len(errors) > 0
        is_error = level.is_error or has_error_pattern
        max_severity = max(
            [p.severity for p, _ in errors] + ([level.priority] if level.is_error else [0])
        )

        return {
            'level': level,
            'level_priority': level.priority,
            'is_error': is_error,
            'errors': errors,
            'max_severity': max_severity,
            'error_categories': list({p.category for p, _ in errors}),
        }

    def update_statistics(self, line: str, source: str = "") -> None:
        """Update statistics with a new log line."""
        current_time = time.time()

        # Update basic stats
        self.stats.total_lines += 1
        self.stats.recent_lines.append((current_time, line))

        if self.stats.first_timestamp == 0.0:
            self.stats.first_timestamp = current_time
        self.stats.last_timestamp = current_time

        # Calculate throughput
        elapsed = self.stats.last_timestamp - self.stats.first_timestamp
        if elapsed > 0:
            self.stats.lines_per_second = self.stats.total_lines / elapsed

        # Analyze line
        analysis = self.analyze_line(line)
        level: LogLevel = analysis['level']
        level_key = level.value
        self.stats.levels[level_key] = self.stats.levels.get(level_key, 0) + 1

        # Update source counts
        if source:
            self.stats.sources[source] = self.stats.sources.get(source, 0) + 1

        # Update error statistics if level is error OR error pattern detected
        if analysis['is_error']:
            self._update_error_stats(line, source, level, current_time, analysis['errors'])
        else:
            # Refresh decay even on non-error lines
            self._update_error_rate(current_time)

    def refresh_rates(self, current_time: float | None = None) -> None:
        """Explicitly refresh rate calculations (useful for UI polling loops)."""
        now = current_time or time.time()
        self._update_error_rate(now)

    def _update_error_stats(
        self,
        line: str,
        source: str,
        level: LogLevel,
        timestamp: float,
        detected_errors: list[tuple[ErrorPattern, str]],
    ) -> None:
        """Update error-specific statistics and categories."""
        self.anomaly_stats.total_errors += 1
        level_key = level.value
        self.anomaly_stats.errors_by_level[level_key] = (
            self.anomaly_stats.errors_by_level.get(level_key, 0) + 1
        )
        self.anomaly_stats.error_timestamps.append(timestamp)

        # Track categories
        for pattern, _ in detected_errors:
            self.anomaly_stats.errors_by_category[pattern.category] = (
                self.anomaly_stats.errors_by_category.get(pattern.category, 0) + 1
            )

        if source:
            self.anomaly_stats.errors_by_source[source] = (
                self.anomaly_stats.errors_by_source.get(source, 0) + 1
            )

        # Track unique error messages (cleaned and truncated)
        error_key = line.strip()[:100]
        self.anomaly_stats.unique_error_messages[error_key] = (
            self.anomaly_stats.unique_error_messages.get(error_key, 0) + 1
        )

        # Update rate tracking
        self._update_error_rate(timestamp)

    def _update_error_rate(self, timestamp: float) -> None:
        """Update error rate calculations using sliding time window."""
        five_minutes_ago = timestamp - 300
        one_minute_ago = timestamp - 60

        # Purge entries older than 5 minutes O(1) per item
        while (
            self.anomaly_stats.error_timestamps
            and self.anomaly_stats.error_timestamps[0] <= five_minutes_ago
        ):
            self.anomaly_stats.error_timestamps.popleft()

        self.anomaly_stats.errors_last_5_minutes = len(self.anomaly_stats.error_timestamps)

        # Count errors in the last 60 seconds
        self.anomaly_stats.errors_last_minute = sum(
            1 for t in self.anomaly_stats.error_timestamps if t > one_minute_ago
        )

        self.anomaly_stats.error_rate_per_minute = float(self.anomaly_stats.errors_last_minute)

        # Detect spikes against baseline
        if self._baseline_error_rate > 0:
            ratio = self.anomaly_stats.error_rate_per_minute / self._baseline_error_rate
            self.anomaly_stats.is_spike = (
                ratio >= self.anomaly_stats.spike_threshold
                and self.anomaly_stats.errors_last_minute >= 5
            )

        # Smooth baseline update every 10 seconds
        if timestamp - self._last_rate_update >= 10.0:
            self._baseline_error_rate = 0.95 * self._baseline_error_rate + 0.05 * max(
                1.0, self.anomaly_stats.error_rate_per_minute
            )
            self._last_rate_update = timestamp

    def get_statistics_summary(self) -> dict[str, Any]:
        """Get a summary of all statistics."""
        return {
            'total_lines': self.stats.total_lines,
            'lines_per_second': round(self.stats.lines_per_second, 2),
            'levels': dict(self.stats.levels),
            'sources': dict(self.stats.sources),
            'total_errors': self.anomaly_stats.total_errors,
            'errors_by_level': dict(self.anomaly_stats.errors_by_level),
            'errors_by_category': dict(self.anomaly_stats.errors_by_category),
            'errors_last_minute': self.anomaly_stats.errors_last_minute,
            'error_rate_per_minute': round(self.anomaly_stats.error_rate_per_minute, 2),
            'is_spike': self.anomaly_stats.is_spike,
            'unique_errors': len(self.anomaly_stats.unique_error_messages),
        }

    def get_top_errors(self, n: int = 10) -> list[tuple[str, int]]:
        """Get the top N most frequent errors."""
        sorted_errors = sorted(
            self.anomaly_stats.unique_error_messages.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return sorted_errors[:n]

    def get_errors_by_category(self) -> dict[str, int]:
        """Get error counts grouped by category."""
        return dict(self.anomaly_stats.errors_by_category)

    def reset_statistics(self) -> None:
        """Reset all statistics."""
        self.stats = LogStatistics()
        self.anomaly_stats = AnomalyStats()
        self._last_rate_update = time.time()
        self._baseline_error_rate = 1.0


class FilterEngine:
    """Advanced filter engine for log filtering."""

    def __init__(self, parser: LogParser | None = None) -> None:
        self._parser = parser or LogParser()
        self._level_filter: LogLevel | None = None
        self._text_filter: str | None = None
        self._source_filter: str | None = None
        self._exclude_patterns: list[re.Pattern[str]] = []

    def set_level_filter(self, level: LogLevel | None) -> None:
        """Set minimum log level filter."""
        self._level_filter = level

    def set_text_filter(self, text: str | None) -> None:
        """Set text search filter (case-insensitive)."""
        self._text_filter = text.lower().strip() if text else None

    def set_source_filter(self, source: str | None) -> None:
        """Set source filter (case-insensitive)."""
        self._source_filter = source.lower().strip() if source else None

    def add_exclude_pattern(self, pattern: str) -> None:
        """Add a regex pattern to exclude."""
        self._exclude_patterns.append(re.compile(pattern, re.IGNORECASE))

    def clear_filters(self) -> None:
        """Clear all filters."""
        self._level_filter = None
        self._text_filter = None
        self._source_filter = None
        self._exclude_patterns.clear()

    def matches(self, entry: Any) -> bool:
        """Check if a log entry matches all active filters."""
        # 1. Level filter check
        if self._level_filter:
            entry_level: LogLevel = LogLevel.INFO
            raw_lvl = getattr(entry, 'level', None)
            if isinstance(raw_lvl, LogLevel):
                entry_level = raw_lvl
            elif isinstance(raw_lvl, str):
                lvl_str = raw_lvl.upper()
                if lvl_str == "WARNING":
                    lvl_str = "WARN"
                entry_level = LogLevel.__members__.get(lvl_str, self._parser.parse_level(raw_lvl))
            elif hasattr(entry, 'raw'):
                entry_level = self._parser.parse_level(entry.raw)

            if entry_level.priority < self._level_filter.priority:
                return False

        # 2. Text filter check
        if self._text_filter:
            search_text = entry.raw if hasattr(entry, 'raw') else str(entry)
            if self._text_filter not in search_text.lower():
                return False

        # 3. Source filter check (Case-insensitive matching)
        if self._source_filter:
            entry_source = entry.source.lower() if hasattr(entry, 'source') else ''
            if self._source_filter not in entry_source:
                return False

        # 4. Exclude patterns
        if self._exclude_patterns:
            search_text = entry.raw if hasattr(entry, 'raw') else str(entry)
            for pattern in self._exclude_patterns:
                if pattern.search(search_text):
                    return False

        return True
