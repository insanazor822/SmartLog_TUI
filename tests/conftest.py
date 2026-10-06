"""pytest configuration and shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smartlog.models import Level, LogEntry  # noqa: E402
from smartlog.parser import LineParser  # noqa: E402


@pytest.fixture(scope="session")
def parser() -> LineParser:
    """A single shared parser instance (it is stateless)."""
    return LineParser()


@pytest.fixture
def make_entry():
    """Factory for LogEntry objects."""

    def factory(
        level: Level = Level.INFO,
        *,
        source: str = "app.log",
        raw: str = "message",
        fingerprint: str = "",
        ts_epoch: float = 0.0,
        line_number: int = 0,
    ) -> LogEntry:
        return LogEntry(
            raw=raw,
            source=source,
            level=level,
            message=raw,
            fingerprint=fingerprint,
            ts_epoch=ts_epoch,
            line_number=line_number,
        )

    return factory