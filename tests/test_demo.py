"""Demo generator tests: output realism, rotation, determinism."""

from __future__ import annotations

import gzip
import time
from pathlib import Path

import pytest

from smartlog.demo import DemoGenerator
from smartlog.knowledge import KnowledgeBase
from smartlog.models import Level
from smartlog.parser import LineParser


@pytest.fixture
def parser() -> LineParser:
    return LineParser()


class TestGenerator:
    def test_writes_parsable_lines(self, tmp_path: Path, parser: LineParser):
        gen = DemoGenerator(tmp_path, base_rate=200.0, interval=0.01, seed=1)
        path = gen.start()
        time.sleep(0.4)
        gen.stop()

        text = path.read_text(encoding="utf-8")
        assert text.strip()
        lines = [l for l in text.splitlines() if l.strip()]
        parsed = [parser.parse(line, str(path), i) for i, line in enumerate(lines, 1)]
        assert all(entry.level for entry in parsed)

    def test_produces_real_error_signatures(self, tmp_path: Path, parser: LineParser):
        gen = DemoGenerator(tmp_path, base_rate=400.0, interval=0.005,
                            burst=True, seed=7)
        path = gen.start()
        time.sleep(0.6)
        gen.stop()

        kb = KnowledgeBase()
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
        matched = sum(1 for line in lines if kb.best(line))
        # The whole point of the demo: real diagnostics should fire on it.
        assert matched > 0, "generated stream matched no diagnostic rules"

    def test_errors_are_generated(self, tmp_path: Path, parser: LineParser):
        gen = DemoGenerator(tmp_path, base_rate=400.0, interval=0.005, seed=3)
        path = gen.start()
        time.sleep(0.5)
        gen.stop()

        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
        parsed = [parser.parse(l, str(path), i) for i, l in enumerate(lines, 1)]
        errors = [e for e in parsed if e.level >= Level.ERROR]
        assert errors, "demo never produced an error"

    def test_mixed_formats(self, tmp_path: Path, parser: LineParser):
        gen = DemoGenerator(tmp_path, base_rate=400.0, interval=0.005, seed=11)
        path = gen.start()
        time.sleep(0.5)
        gen.stop()

        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l]
        assert any(l.lstrip().startswith("{") for l in lines), "no JSON lines emitted"

    def test_rotation_creates_gzip(self, tmp_path: Path):
        gen = DemoGenerator(tmp_path, base_rate=400.0, interval=0.005,
                            rotate_every=1500, min_rotate_gap=0.05,
                            gzip_rotated=True, seed=5)
        path = gen.start()
        time.sleep(1.2)
        gen.stop()

        rotated = list(tmp_path.glob("*.gz"))
        assert rotated, "no rotated gz file produced"
        with gzip.open(rotated[0], "rt", encoding="utf-8") as handle:
            assert handle.read().strip()
        assert path.exists(), "live file should be reopened after rotation"
        assert gen.rotations >= 1

    def test_counts_lines_written(self, tmp_path: Path):
        gen = DemoGenerator(tmp_path, base_rate=300.0, interval=0.005, seed=2)
        gen.start()
        time.sleep(0.4)
        gen.stop()
        assert gen.lines_written > 0

    def test_stop_is_idempotent(self, tmp_path: Path):
        gen = DemoGenerator(tmp_path, base_rate=50.0, interval=0.01)
        gen.start()
        time.sleep(0.15)
        gen.stop()
        gen.stop()      # must not raise

    def test_deterministic_with_seed(self, tmp_path: Path):
        """Same seed + same elapsed budget must produce the same line sequence."""
        import re

        def sample(directory: Path) -> list[str]:
            gen = DemoGenerator(directory, base_rate=200.0, interval=0.005,
                                seed=42, rotate_every=0)
            path = gen.start()
            time.sleep(0.25)
            gen.stop()
            # Strip the timestamp and the per-directory bootstrap line: neither
            # is seed-derived.
            return [re.sub(r"^\S+ \S+ ", "", line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if "demo stream ready" not in line]

        first = sample(tmp_path / "a")
        second = sample(tmp_path / "b")
        assert first[:5] == second[:5]
        assert len(first) > 0


class TestDemoWithReader:
    @pytest.mark.asyncio
    async def test_reader_follows_demo_stream(self, tmp_path: Path, parser):
        """End-to-end: generator -> tailer -> parser must produce usable entries."""
        import asyncio

        from smartlog.reader import LogStream
        from smartlog.stats import LogStats

        gen = DemoGenerator(tmp_path, base_rate=200.0, interval=0.01, seed=9)
        path = gen.start()

        stream = LogStream([path], buffer_size=5000, start_at_end=True)
        await stream.start()
        stats = LogStats()
        try:
            deadline = time.monotonic() + 4.0
            while stats.total_lines < 50 and time.monotonic() < deadline:
                try:
                    batch = await asyncio.wait_for(stream._queue.get(), timeout=0.4)
                except TimeoutError:
                    continue
                stream._queue.task_done()
                stats.record_many(batch)

            snapshot = stats.snapshot()
            assert stats.total_lines >= 50, f"only saw {stats.total_lines} lines"
            assert snapshot.lines_per_sec > 0
            assert snapshot.level_counts
        finally:
            await stream.stop()
            gen.stop()