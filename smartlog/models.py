"""Core domain models for SmartLog.

Single source of truth for log levels, log entries and diagnosis types.
Everything downstream (reader, stats, filters, exporters, UI) depends only on
these models, so the parsing/normalisation logic lives in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

__all__ = [
    "LEVEL_ALIASES",
    "LEVEL_COLORS",
    "Level",
    "LogEntry",
    "lookup_level",
    "parse_level_token",
]


class Level(IntEnum):
    """Numeric log levels.

    ``IntEnum`` (instead of ``str, Enum``) makes comparisons and filtering O(1)
    and free of the string normalisation that the previous implementation had to
    redo on every single filter pass.

    Values deliberately start at 1 so that every member is truthy — a ``0``
    level would silently break ``if entry.level:`` style checks.
    """

    TRACE = 1
    DEBUG = 10
    INFO = 20
    NOTICE = 25
    WARN = 30
    ERROR = 40
    CRITICAL = 50
    FATAL = 60

    @property
    def label(self) -> str:
        return LEVEL_LABELS[self]

    @classmethod
    def from_any(cls, value: object, default: Level | None = None) -> Level:
        """Coerce an arbitrary object into a :class:`Level` without raising."""
        fallback = default if default is not None else cls.INFO
        if isinstance(value, Level):
            return value
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                return fallback
        if value is None:
            return fallback
        return parse_level_token(str(value), default=fallback)


LEVEL_LABELS: dict[Level, str] = {
    Level.TRACE: "TRACE",
    Level.DEBUG: "DEBUG",
    Level.INFO: "INFO",
    Level.NOTICE: "NOTICE",
    Level.WARN: "WARN",
    Level.ERROR: "ERROR",
    Level.CRITICAL: "CRITICAL",
    Level.FATAL: "FATAL",
}

# Rich colour used by both the TUI and the HTML/text exporters.
LEVEL_COLORS: dict[Level, str] = {
    Level.TRACE: "dim white",
    Level.DEBUG: "dim cyan",
    Level.INFO: "green",
    Level.NOTICE: "cyan",
    Level.WARN: "yellow",
    Level.ERROR: "red",
    Level.CRITICAL: "bold red",
    Level.FATAL: "bold white on red",
}

# Vendor-specific level names mapped onto the canonical set.
LEVEL_ALIASES: dict[str, Level] = {
    "TRACE": Level.TRACE,
    "VERBOSE": Level.TRACE,
    "V": Level.TRACE,
    "DEBUG": Level.DEBUG,
    "DBG": Level.DEBUG,
    "D": Level.DEBUG,
    "INFO": Level.INFO,
    "INFORMATION": Level.INFO,
    "NOTICE": Level.NOTICE,
    "I": Level.INFO,
    "WARN": Level.WARN,
    "WARNING": Level.WARN,
    "ERR": Level.ERROR,
    "ERROR": Level.ERROR,
    "E": Level.ERROR,
    "CRITICAL": Level.CRITICAL,
    "CRIT": Level.CRITICAL,
    "FATAL": Level.FATAL,
    "EMERG": Level.FATAL,
    "EMERGENCY": Level.FATAL,
    "ALERT": Level.FATAL,
    "PANIC": Level.FATAL,
}

_LEVEL_DECORATIONS = "[]()<>:-_,; \t"

# Pre-split on whitespace: in the overwhelming majority of lines the level is
# one of the first handful of tokens, so we avoid running the full regex.
_TOKEN_LEVELS = frozenset(LEVEL_ALIASES)


def parse_level_token(token: str, default: Level = Level.INFO) -> Level:
    """Map a single token to a :class:`Level` (fast path, no regex when possible)."""
    found = lookup_level(token)
    return default if found is None else found


def lookup_level(token: str) -> Level | None:
    """Like :func:`parse_level_token` but returns ``None`` for unknown tokens.

    The distinction matters when parsing ``TS [ERROR] [api] message``: the parser
    must be able to say "this bracket is *not* a level" so it can treat it as a
    source name instead.
    """
    upper = token.upper()
    if not upper:
        return None
    level = LEVEL_ALIASES.get(upper)
    if level is not None:
        return level
    # Tolerate decorations such as "WARN:" or "[ERROR]". ``str.strip`` is used
    # instead of a regex because this runs several times per log line.
    stripped = upper.strip(_LEVEL_DECORATIONS)
    if stripped == upper:
        return None
    return LEVEL_ALIASES.get(stripped)


@dataclass(slots=True)
class LogEntry:
    """A single parsed log line.

    ``__slots__`` matters here: the ring buffer keeps tens of thousands of these
    alive, and the previous plain dataclass carried a per-instance ``__dict__``.
    """

    raw: str
    source: str
    level: Level = Level.INFO
    timestamp: str = ""
    message: str = ""
    line_number: int = 0
    ts_epoch: float = 0.0
    fingerprint: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.level >= Level.ERROR

    @property
    def level_name(self) -> str:
        return self.level.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "ts_epoch": self.ts_epoch,
            "level": self.level.label,
            "source": self.source,
            "line_number": self.line_number,
            "message": self.message,
            "raw": self.raw,
            "fingerprint": self.fingerprint,
            "meta": self.meta,
        }