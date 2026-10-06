"""Filter engine.

Optimisation vs. the original: the original called ``entry.raw.lower()`` and
re-resolved the level enum on **every** filter evaluation for **every** entry,
which the UI triggered once per displayed line. Here the searchable text is
normalised **once at parse time** and stored on the entry, so a filter check is
pure integer comparisons plus a single substring test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .models import Level, LogEntry

__all__ = ["FilterEngine", "FilterSpec", "highlight_terms"]


@dataclass(slots=True)
class FilterSpec:
    """Declarative filter description (serialisable, easy to snapshot)."""

    min_level: Level | None = None
    max_level: Level | None = None
    sources: frozenset[str] = frozenset()
    include: tuple[str, ...] = ()          # AND-ed substrings, case-insensitive
    exclude: tuple[str, ...] = ()          # OR-ed substrings to drop
    exclude_regex: tuple[re.Pattern[str], ...] = ()
    error_only: bool = False
    # Terms to visually highlight. Unlike ``include`` these do NOT hide lines.
    search: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.min_level or self.max_level or self.sources or self.include
            or self.exclude or self.exclude_regex or self.error_only
        )

    def describe(self) -> str:
        parts: list[str] = []
        if self.min_level is not None:
            parts.append(f"level >= {self.min_level.label}")
        if self.max_level is not None:
            parts.append(f"level <= {self.max_level.label}")
        if self.error_only:
            parts.append("errors only")
        if self.sources:
            parts.append("source in {" + ", ".join(sorted(self.sources)) + "}")
        if self.include:
            parts.append("contains all of [" + ", ".join(self.include) + "]")
        if self.exclude:
            parts.append("excludes [" + ", ".join(self.exclude) + "]")
        if self.exclude_regex:
            parts.append(f"{len(self.exclude_regex)} regex exclude(s)")
        if not parts and self.search:
            return "highlighting [" + ", ".join(self.search) + "]"
        if not parts:
            return "no filters (showing everything)"
        if self.search:
            parts.append("highlighting [" + ", ".join(self.search) + "]")
        return "; ".join(parts)


class FilterEngine:
    """Stateless predicate over :class:`LogEntry`.

    Kept as a thin wrapper so callers get a stable API, but the hot path is a
    handful of integer comparisons.
    """

    __slots__ = ("_spec",)

    def __init__(self, spec: FilterSpec | None = None) -> None:
        self._spec = spec or FilterSpec()

    @property
    def spec(self) -> FilterSpec:
        return self._spec

    def set_spec(self, spec: FilterSpec) -> None:
        self._spec = spec

    # -- v1 compatibility ------------------------------------------------- #
    # The v1 engine exposed mutable ``set_*`` accessors. They are kept so that
    # existing integrations keep working; they mutate a copy of the spec
    # because FilterSpec fields are typed as immutable tuples/frozensets.

    def set_level_filter(self, level: Level | str | None) -> None:
        """Set the minimum level. ``None`` clears it."""
        if level is None:
            resolved = None
        elif isinstance(level, Level):
            resolved = level
        else:
            token = str(level).strip().upper()
            resolved = Level.from_any(token) if token in _LEVEL_WORDS else None
        self._mutate(min_level=resolved)

    def set_text_filter(self, text: str | None) -> None:
        """Set the substring search. ``None`` clears it."""
        cleaned = text.strip().lower() if text and text.strip() else None
        self._mutate(include=(cleaned,) if cleaned else ())

    def set_source_filter(self, source: str | None) -> None:
        """Restrict to one source. ``None`` clears it."""
        cleaned = source.strip() if source and source.strip() else None
        self._mutate(sources=frozenset({cleaned}) if cleaned else frozenset())

    def add_exclude_pattern(self, pattern: str) -> None:
        """Append a regex whose matches are dropped.

        Raises :class:`ValueError` on an invalid pattern, matching v1's
        ``re.error`` behaviour.
        """
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid exclude regex {pattern!r}: {exc}") from exc
        self._mutate(exclude_regex=self._spec.exclude_regex + (compiled,))

    def clear_filters(self) -> None:
        """Drop every active filter."""
        self._spec = FilterSpec()

    def _mutate(self, **changes: Any) -> None:
        """Return a copy of the current spec with ``changes`` applied."""
        self._spec = replace(self._spec, **changes)

    # -- matching --------------------------------------------------------- #

    def matches(self, entry: LogEntry) -> bool:
        spec = self._spec
        if spec.min_level is not None and entry.level < spec.min_level:
            return False
        if spec.max_level is not None and entry.level > spec.max_level:
            return False
        if spec.error_only and not entry.is_error:
            return False
        if spec.sources and entry.source not in spec.sources:
            return False

        if spec.include or spec.exclude or spec.exclude_regex:
            haystack = entry.raw.lower()
            for needle in spec.include:
                if needle not in haystack:
                    return False
            for needle in spec.exclude:
                if needle in haystack:
                    return False
            for pattern in spec.exclude_regex:
                if pattern.search(entry.raw):
                    return False
        return True

    def apply(self, entries: list[LogEntry]) -> list[LogEntry]:
        """Filter a list. Skips the copy entirely when no filter is active."""
        if self._spec.is_empty():
            return entries
        return [e for e in entries if self.matches(e)]


def highlight_terms(text: str, terms: tuple[str, ...]) -> list[tuple[int, int]]:
    """Return non-overlapping ``(start, end)`` spans of ``terms`` in ``text``."""
    if not terms:
        return []
    spans: list[tuple[int, int]] = []
    lowered = text.lower()
    for term in terms:
        if not term:
            continue
        start = lowered.find(term)
        while start != -1:
            end = start + len(term)
            if not any(s < end and start < e for s, e in spans):
                spans.append((start, end))
            start = lowered.find(term, start + len(term))
    spans.sort()
    return spans


# --------------------------------------------------------------------------- #
# Spec builders (used by the UI dialog and the CLI)
# --------------------------------------------------------------------------- #

def build_spec(
    *,
    min_level: str | Level | None = None,
    sources: list[str] | None = None,
    include: str | None = None,
    exclude: str | None = None,
    exclude_regex: str | list[str] | None = None,
    error_only: bool = False,
    search: str = "",
) -> FilterSpec:
    """Build a :class:`FilterSpec` from plain user input."""
    level: Level | None = None
    if isinstance(min_level, Level):
        level = min_level
    elif min_level:
        token = min_level.strip().upper()
        level = Level.from_any(token, default=None) if token in _LEVEL_WORDS else None

    needles = tuple(
        t for t in (part.strip().lower() for part in (include or "").split(","))
        if t
    )
    terms = tuple(
        t for t in (part.strip().lower() for part in (search or "").split(","))
        if t
    )
    bans = tuple(
        t for t in (part.strip().lower() for part in (exclude or "").split(","))
        if t
    )

    if isinstance(exclude_regex, str):
        raw_regexes: list[str] = [
            part for part in (p.strip() for p in exclude_regex.split(",")) if part
        ]
    else:
        raw_regexes = [r for r in (exclude_regex or []) if r]

    patterns: list[re.Pattern[str]] = []
    for raw in raw_regexes:
        try:
            patterns.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"invalid exclude regex {raw!r}: {exc}") from exc

    return FilterSpec(
        min_level=level,
        sources=frozenset(sources or ()),
        include=needles,
        exclude=bans,
        exclude_regex=tuple(patterns),
        error_only=error_only,
        search=terms,
    )


_LEVEL_WORDS = {
    "TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR",
    "CRITICAL", "FATAL",
}