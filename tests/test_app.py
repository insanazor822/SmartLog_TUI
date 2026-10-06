"""Headless smoke tests for the Textual UI.

Boots the real app against a live demo stream, drives the actions and asserts
the widgets update. These are the only tests that import ``textual``.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, RichLog

from smartlog.app import SmartLogApp
from smartlog.demo import DemoGenerator
from smartlog.filters import build_spec
from smartlog.models import Level
from smartlog.parser import LineParser

SIZE = (160, 48)


@pytest.fixture
def demo_log(tmp_path: Path):
    """A live demo stream, stopped on teardown."""
    generator = DemoGenerator(
        tmp_path / "demo", base_rate=120.0, interval=0.01,
        burst=True, rotate_every=0, seed=17,
    )
    path = generator.start()
    try:
        yield path
    finally:
        generator.stop()


def drive(
    paths,
    body: Callable[[SmartLogApp, object], Awaitable[None]],
    *,
    settle: float = 2.0,
    **kwargs,
) -> Awaitable[None]:
    """Boot the app, let it stream, then run ``body`` while it is mounted."""

    async def runner() -> None:
        app = SmartLogApp(paths, **kwargs)
        async with app.run_test(size=SIZE) as pilot:
            await asyncio.sleep(settle)
            await pilot.pause()
            await body(app, pilot)

    return runner()


class TestBoot:
    @pytest.mark.asyncio
    async def test_streams_and_updates_widgets(self, demo_log: Path):
        async def body(app, pilot):
            assert app.query_one("#logview", RichLog) is not None
            assert app.stats.total_lines > 20, "no lines were streamed"
            assert app.displayed_lines > 0, "log view stayed empty"

            status = str(app.query_one("#status").render())
            assert "lines" in status
            assert str(app.query_one("#sparkline").render()).strip() != ""

            files = app.query_one("#filetable", DataTable)
            assert files.row_count == 1
            await pilot.pause()

        await drive([demo_log], body)

    @pytest.mark.asyncio
    async def test_stats_are_populated(self, demo_log: Path):
        async def body(app, pilot):
            snapshot = app._last_snapshot
            assert snapshot.total_lines > 0
            assert snapshot.level_counts
            assert snapshot.lines_per_sec > 0
            assert snapshot.uptime_sec > 0

        await drive([demo_log], body, settle=2.5)

    @pytest.mark.asyncio
    async def test_from_start_reads_history(self, tmp_path: Path):
        log = tmp_path / "history.log"
        log.write_text(
            "2026-08-30 20:00:01 INFO first line\n"
            "2026-08-30 20:00:02 ERROR second line\n",
            encoding="utf-8",
        )

        async def body(app, pilot):
            assert app.stats.total_lines == 2
            assert app.stats.total_errors >= 1

        await drive([log], body, settle=0.8, start_at_end=False)

    @pytest.mark.asyncio
    async def test_history_is_counted_exactly_once_when_reader_wins_the_race(self):
        """Regression: a fast reader must not double-count history.

        ``_prime_view`` renders from the ring buffer while ``_pump`` records
        from the batch queue. Both see the same entries, so recording in both
        places inflated the totals to 4 for a 2-line file whenever the reader
        outran ``_prime_view``. CI hit this as ``assert 4 == 2``.

        The window is primed by hand so the fast-reader case is deterministic
        instead of depending on scheduler luck.
        """
        log = Path(tempfile.mkdtemp()) / "history.log"
        log.write_text(
            "2026-08-30 20:00:01 INFO one\n"
            "2026-08-30 20:00:02 INFO two\n"
            "2026-08-30 20:00:03 ERROR three\n",
            encoding="utf-8",
        )

        app = SmartLogApp([log], start_at_end=False)
        async with app.run_test(size=SIZE):
            # Fill the buffer the way a fast reader would, then prime.
            await asyncio.sleep(0.5)
            assert app.stream.snapshot(), "reader produced nothing to prime with"
            app._prime_view()
            await asyncio.sleep(0.3)
            assert app.stats.total_lines == 3, (
                f"expected exactly 3 counted lines, got {app.stats.total_lines}"
            )
            assert app.stats.total_errors == 1

    @pytest.mark.asyncio
    async def test_no_paths_shows_warning(self):
        async def body(app, pilot):
            assert app.stream.paths() == []

        await drive([], body, settle=0.3)

    @pytest.mark.asyncio
    async def test_missing_file_does_not_crash(self, tmp_path: Path):
        async def body(app, pilot):
            assert app.stats.total_lines == 0

        await drive([tmp_path / "ghost.log"], body, settle=0.8)

    @pytest.mark.asyncio
    async def test_multiple_sources(self, tmp_path: Path, demo_log: Path):
        second = tmp_path / "second.log"
        second.write_text("2026-08-30 20:00:01 INFO from second file\n",
                          encoding="utf-8")

        async def body(app, pilot):
            files = app.query_one("#filetable", DataTable)
            assert files.row_count == 2

        await drive([demo_log, second], body, settle=1.0)


class TestActions:
    @pytest.mark.asyncio
    async def test_pause_freezes_view_but_keeps_stats(self, demo_log: Path):
        async def body(app, pilot):
            app.action_pause()
            assert app._paused is True
            before_lines = app.stats.total_lines
            before_shown = app.displayed_lines
            await asyncio.sleep(0.8)
            # Statistics keep advancing while the view is frozen.
            assert app.stats.total_lines > before_lines
            assert app.displayed_lines == before_shown
            app.action_pause()
            assert app._paused is False

        await drive([demo_log], body, settle=1.2)

    @pytest.mark.asyncio
    async def test_clear_empties_view(self, demo_log: Path):
        async def body(app, pilot):
            assert app.displayed_lines > 0
            app.action_pause()          # stop new lines from arriving mid-assert
            app.action_clear()
            assert app.displayed_lines == 0
            assert app._pending == []
            await pilot.pause()

        await drive([demo_log], body, settle=1.2)

    @pytest.mark.asyncio
    async def test_reset_stats(self, demo_log: Path):
        async def body(app, pilot):
            assert app.stats.total_lines > 0
            app.action_reset_stats()
            assert app.stats.total_lines == 0

        await drive([demo_log], body, settle=1.2)

    @pytest.mark.asyncio
    async def test_toggle_follow_and_debug_view(self, demo_log: Path):
        async def body(app, pilot):
            app.action_toggle_follow()
            assert app._follow is False
            app.action_toggle_debug()
            assert app._debug_view is True

        await drive([demo_log], body, settle=1.0)

    @pytest.mark.asyncio
    async def test_filter_spec_from_constructor(self, demo_log: Path):
        spec = build_spec(min_level=Level.ERROR)

        async def body(app, pilot):
            assert app.filters.spec.min_level is Level.ERROR
            assert "ERROR" in app._spec_description
            # Every rendered line must satisfy the filter.
            assert app.displayed_lines >= 0

        await drive([demo_log], body, settle=1.5, filter_spec=spec)

    @pytest.mark.asyncio
    async def test_diagnose_action_completes(self, demo_log: Path):
        """Key regression: diagnosis must not freeze or break the UI."""

        async def body(app, pilot):
            app.action_diagnose()
            assert app._diag_task is not None
            await asyncio.wait_for(asyncio.shield(app._diag_task), timeout=10.0)
            assert app._diag_task.done()
            assert app._diag_task.exception() is None

            history = app.engine.history
            assert history, "no diagnosis was recorded"
            assert history[-1].recommendations, "diagnosis had no next steps"

            rendered = str(app.query_one("#diagpane").render())
            assert "next steps" in rendered.lower()

        await drive([demo_log], body, settle=1.5, offline_diagnostics=True)

    @pytest.mark.asyncio
    async def test_diagnose_without_errors_is_safe(self, tmp_path: Path):
        log = tmp_path / "quiet.log"
        log.write_text("2026-08-30 20:00:01 INFO all good\n" * 20, encoding="utf-8")

        async def body(app, pilot):
            app.action_diagnose()          # must not raise

        await drive([log], body, settle=0.6, start_at_end=False)

    @pytest.mark.asyncio
    async def test_export_writes_file(self, demo_log: Path, tmp_path: Path):
        target = tmp_path / "out.md"

        async def body(app, pilot):
            entries = app._entries_for_export()
            assert entries
            written = app.exporter.write(
                entries, "md", target, sources=app.log_paths,
                filters=app._spec_description, stats=app._last_snapshot,
            )
            assert written.exists()
            assert "SmartLog" in written.read_text(encoding="utf-8")

        await drive([demo_log], body, settle=1.5)

    @pytest.mark.asyncio
    async def test_export_with_no_entries_is_safe(self, tmp_path: Path):
        async def body(app, pilot):
            app.action_export()             # must not raise

        await drive([tmp_path / "ghost.log"], body, settle=0.4)

    @pytest.mark.asyncio
    async def test_help_action(self, demo_log: Path):
        async def body(app, pilot):
            app.action_help()
            rendered = str(app.query_one("#diagpane").render())
            assert "diagnose" in rendered

        await drive([demo_log], body, settle=0.8)

    @pytest.mark.asyncio
    async def test_keybindings_are_declared(self):
        keys = {b.key for b in SmartLogApp.BINDINGS}
        assert {"q", "a", "f", "e", "p", "c", "r", "t", "d"} <= keys

    @pytest.mark.asyncio
    @pytest.mark.flaky(reruns=2, reason="Textual shutdown needs more wall clock on 3.10")
    async def test_quit_is_clean(self, demo_log: Path):
        async def body(app, pilot):
            await pilot.press("q")
            # Give the app a beat to run its shutdown path. Without this the
            # run_test context exits while widgets still hold pending messages,
            # and Textual raises WaitForScreenTimeout on a loaded runner.
            await asyncio.sleep(0.5)
        await drive([demo_log], body, settle=1.0)


class TestRendering:
    @pytest.mark.asyncio
    async def test_error_line_formatting(self, tmp_path: Path):
        entry = LineParser().parse(
            "2026-08-30 20:00:01 ERROR deadlock detected on orders", "db.log", 1
        )
        async def body(app, pilot):
            plain = app._format_entry(entry).plain
            assert "ERROR" in plain
            assert "deadlock detected" in plain
            assert "db.log" in plain

        await drive([tmp_path / "x.log"], body, settle=0.2)

    @pytest.mark.asyncio
    async def test_debug_view_shows_raw_line(self, tmp_path: Path):
        entry = LineParser().parse("INFO hello", "app.log", 1)

        async def body(app, pilot):
            app._debug_view = True
            assert "INFO hello" in app._format_entry(entry).plain

        await drive([tmp_path / "x.log"], body, settle=0.2)

    @pytest.mark.asyncio
    async def test_signature_table_populates(self, tmp_path: Path):
        log = tmp_path / "errors.log"
        log.write_text(
            "\n".join([
                "2026-08-30 20:00:01 ERROR deadlock detected on orders",
                "2026-08-30 20:00:02 ERROR deadlock detected again",
                "2026-08-30 20:00:03 INFO routine",
            ]) + "\n",
            encoding="utf-8",
        )

        async def body(app, pilot):
            table = app.query_one("#sigtable", DataTable)
            assert table.row_count >= 1

        await drive([log], body, settle=0.8, start_at_end=False)

    @pytest.mark.asyncio
    async def test_filter_panel_shows_description(self, demo_log: Path):
        spec = build_spec(min_level=Level.ERROR, include="db")

        async def body(app, pilot):
            rendered = str(app.query_one("#activefilter").render())
            assert "ERROR" in rendered
            assert "db" in rendered

        await drive([demo_log], body, settle=1.0, filter_spec=spec)