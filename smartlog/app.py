"""Textual TUI.

The original wrote to ``RichLog`` once **per incoming line**, re-ran the parser
twice per line, and blocked the event loop during diagnosis. This version:

* consumes the reader in **batches** on a fixed tick and re-renders once,
* parses each line **once** (``LogEntry`` is the single representation),
* runs diagnosis in an ``asyncio`` worker so the UI never freezes,
* keeps the ring buffer as the only store and derives the visible list by
  filtering, so memory is not duplicated,
* adds an error-focused view, top-signature list, pause, follow toggle,
  copy/export, and a searchable filter dialog.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .diagnostics import Diagnosis, DiagnosticEngine
from .exporters import Exporter, default_filename
from .filters import FilterEngine, FilterSpec, build_spec, highlight_terms
from .models import LEVEL_COLORS, Level, LogEntry
from .reader import LogStream
from .stats import LogStats, StatsSnapshot

__all__ = ["SmartLogApp", "run_tui"]

UI_TICK = 0.12          # seconds between render passes
MAX_RENDERED_LINES = 4000
RESUME_BACKFILL = 2000  # lines replayed from the buffer when un-pausing
HIGHLIGHT_STYLE = "black on yellow"   # search-term highlight


class Sparkline(Static):
    """Live rate sparkline."""

    def refresh_spark(self, text: str, label: str = "") -> None:
        self.update(Text(f"{label} {text}", style="cyan") if text else Text(""))


class StatusPanel(Static):
    """Live counters and top signatures."""

    def refresh_stats(self, snap: StatsSnapshot, extra: str = "") -> None:
        # Build one Text and update once: calling ``update`` twice would leave
        # only the last chunk visible.
        text = Text()
        text.append("lines  ", style="dim")
        text.append(f"{snap.total_lines:,}", style="bold white")
        text.append("  rate  ", style="dim")
        text.append(f"{snap.lines_per_sec:.1f}/s", style="bold cyan")
        text.append("  errors  ", style="dim")
        text.append(f"{snap.total_errors:,}", style="bold red")
        text.append(f" {snap.error_rate_per_min:.1f}/min", style="red")
        text.append("  view  ", style="dim")
        text.append(extra, style="bold white")
        if snap.dropped_lines:
            text.append("  dropped ", style="dim")
            text.append(f"{snap.dropped_lines:,}", style="yellow")

        if snap.spike_active:
            text.append(
                f"\n  ANOMALY: error rate {snap.spike_ratio:.1f}x baseline",
                style="bold red on dark_red",
            )

        if snap.top_fingerprints:
            text.append("\nsignatures\n", style="dim bold")
            peak = snap.top_fingerprints[0][1] or 1
            for name, count in snap.top_fingerprints[:8]:
                bar = "█" * max(1, round(count / peak * 20))
                text.append(f"  {name:<20}{count:>7} ", style="white")
                text.append(bar, style="red")
        self.update(text)


class FilterPanel(Static):
    """Active-filter summary line."""

    def refresh_filter(self, description: str) -> None:
        style = "dim" if description.startswith("no filters") else "bold yellow"
        self.update(Text(f"filter: {description}", style=style))


class DiagnosisPanel(Static):
    """Renders a :class:`Diagnosis` (or a spinner while one is in flight)."""

    def show_idle(self, hint: str) -> None:
        self.update(Text("no diagnosis yet\n\n" + hint, style="dim"))

    def show_busy(self, message: str) -> None:
        self.update(Text(f"diagnosing…\n\n{message}", style="bold cyan"))

    def show(self, diagnosis: Diagnosis) -> None:
        t = Text()
        style = {"low": "dim", "medium": "yellow", "high": "red",
                 "very_high": "bold red"}.get(diagnosis.confidence, "white")
        t.append(f"{diagnosis.summary}\n", style=f"bold {style}")
        t.append("confidence  ", style="dim")
        t.append(f"{diagnosis.confidence}\n", style=style)
        t.append("source      ", style="dim")
        t.append(f"{diagnosis.source} ({diagnosis.elapsed_ms:.0f} ms)\n")
        t.append(f"\nroot cause\n{diagnosis.root_cause}\n", style="white")

        if diagnosis.evidence:
            t.append("\nevidence\n", style="dim bold")
            for line in diagnosis.evidence[:8]:
                t.append(f"  • {line}\n", style="dim white")

        if diagnosis.recommendations:
            t.append("\nnext steps\n", style="dim bold")
            for i, rec in enumerate(diagnosis.recommendations, 1):
                sev = {"low": "dim", "medium": "yellow", "high": "red",
                       "critical": "bold red"}.get(rec.severity, "white")
                t.append(f"  {i}. {rec.title} ", style=sev)
                if rec.estimated_time:
                    t.append(f"({rec.estimated_time})\n", style="dim")
                else:
                    t.append("\n")
                if rec.description:
                    t.append(f"     {rec.description}\n", style="white")
                if rec.command:
                    t.append(f"     $ {rec.command}\n", style="bold green")

        if diagnosis.prevention:
            t.append("\nprevention\n", style="dim bold")
            for tip in diagnosis.prevention[:6]:
                t.append(f"  • {tip}\n", style="dim white")

        if diagnosis.docs:
            t.append("\nreferences\n", style="dim bold")
            for doc in diagnosis.docs[:4]:
                t.append(f"  • {doc}\n", style="dim blue underline")

        self.update(t)


class FilterDialog(ModalScreen[dict[str, Any] | None]):
    """Interactive filter builder."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+r", "reset", "Reset all"),
    ]

    def __init__(self, *, focus: str | None = None) -> None:
        super().__init__()
        self._focus = focus

    CSS = """
    FilterDialog { align: center middle; }
    #box {
        width: 74; height: auto; padding: 1 2;
        background: $panel; border: round $accent;
    }
    #box Label { margin-top: 1; }
    #levels { height: 3; }
    .row { height: auto; margin-top: 1; align-horizontal: right; }
    """

    LEVELS = [(f"{lv.label} ({int(lv)})", lv.label) for lv in sorted(Level)]

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("SmartLog — filters")
            yield Label("[dim]press esc to cancel · ctrl+r to reset[/dim]")
            yield Label("minimum level (blank = all)")
            yield Select(
                [("[any]", "")] + self.LEVELS,
                value="",
                allow_blank=True,
                id="min_level",
            )
            yield Label("include (comma separated substrings, AND)")
            yield Input(placeholder="timeout, /api/v1", id="include")
            yield Label("exclude (comma separated substrings)")
            yield Input(placeholder="healthz, /metrics", id="exclude")
            yield Label("exclude regex (comma separated)")
            yield Input(placeholder="^DEBUG, \\bGET /static", id="exclude_regex")
            yield Label("sources (comma separated, blank = all)")
            yield Input(placeholder="app.log, nginx", id="sources")
            yield Label("[dim]highlight (comma separated - does not hide lines)[/dim]")
            yield Input(placeholder="timeout, deadlock", id="search")
            yield Checkbox("errors only", value=False, id="errors_only")
            with Horizontal(classes="row"):
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Reset", variant="warning", id="reset")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self._focus:
            widget = self._value(f"#{self._focus}", Input)
            if widget is not None:
                widget.focus()

    def _value(self, selector: str, cls: type) -> Any:
        with contextlib.suppress(Exception):
            return self.query_one(selector, cls)
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "apply":
                self.dismiss(self._collect())
            case "reset":
                self.dismiss({"__reset__": True})
            case _:
                self.dismiss(None)

    def _collect(self) -> dict[str, Any]:
        def text(sel: str) -> str:
            widget = self._value(sel, Input)
            return widget.value if widget is not None else ""

        level_widget = self._value("#min_level", Select)
        level = level_widget.value if level_widget is not None else ""
        level = "" if level is Select.BLANK else level
        checkbox = self._value("#errors_only", Checkbox)
        return {
            "min_level": level or None,
            "include": text("#include"),
            "exclude": text("#exclude"),
            "exclude_regex": text("#exclude_regex"),
            "sources": text("#sources"),
            "search": text("#search"),
            "error_only": bool(checkbox.value) if checkbox is not None else False,
        }

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_reset(self) -> None:
        self.dismiss({"__reset__": True})


class ExportDialog(ModalScreen[dict[str, Any] | None]):
    """Format + destination chooser."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ExportDialog { align: center middle; }
    #box { width: 72; height: auto; padding: 1 2;
           background: $panel; border: round $accent; }
    #formats { height: 4; }
    .row { height: auto; margin-top: 1; align-horizontal: right; }
    """

    def __init__(self, *, total: int, filters: str, default_dir: Path) -> None:
        super().__init__()
        self._total = total
        self._filters = filters
        self._default_dir = default_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(f"Export {self._total:,} visible entries")
            yield Label(f"[dim]active filters: {self._filters}[/dim]")
            yield Label("format")
            with RadioSet(id="formats"):
                yield RadioButton("Markdown (.md)", value=True, id="md")
                yield RadioButton("JSON (.json)", id="json")
                yield RadioButton("Plain text (.txt)", id="txt")
                yield RadioButton("HTML report (.html)", id="html")
            yield Label("destination")
            yield Input(value=str(self._default_dir), id="dest")
            yield Label("[dim]/path/to/report.md switches format by suffix[/dim]")
            with Horizontal(classes="row"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Copy to clipboard", variant="success", id="copy")
                yield Button("Cancel", id="cancel")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        fmt = event.pressed.id or "md"
        dest = self._value("#dest", Input)
        if dest is not None:
            dest.value = str(self._default_dir / default_filename(fmt))

    def _value(self, selector: str, cls: type) -> Any:
        with contextlib.suppress(Exception):
            return self.query_one(selector, cls)
        return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "save":
                self.dismiss({"mode": "save", "fmt": self._fmt(),
                              "dest": self._dest()})
            case "copy":
                self.dismiss({"mode": "copy", "fmt": self._fmt()})
            case _:
                self.dismiss(None)

    def _fmt(self) -> str:
        group = self._value("#formats", RadioSet)
        if group is not None and group.pressed_button is not None:
            return group.pressed_button.id or "md"
        return "md"

    def _dest(self) -> str:
        widget = self._value("#dest", Input)
        return (widget.value if widget is not None else "").strip() or "."

    def action_cancel(self) -> None:
        self.dismiss(None)


class SmartLogApp(App[None]):
    """Main application."""

    CSS = """
    Screen { background: $surface; }
    Header { dock: top; }
    Footer { dock: bottom; }
    #body { height: 1fr; }
    #logpane { border: round $primary; height: 1fr; }
    #side {
        width: 44; border: round $secondary; padding: 0 1;
        height: 1fr; overflow-y: auto;
    }
    #sparkline { height: 1; padding: 0 1; }
    #status { height: auto; padding: 0 1; border-bottom: solid $panel; }
    #activefilter { height: 1; padding: 0 1; }
    #diagpane { border-top: solid $accent; height: 1fr; padding: 0 1; }
    #tabs { height: 1fr; }
    RichLog { height: 1fr; }
    #diagscroll { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("a", "diagnose", "Diagnose"),
        Binding("f", "filter", "Filter"),
        Binding("e", "export", "Export"),
        Binding("p", "pause", "Pause"),
        Binding("c", "clear", "Clear"),
        Binding("r", "reset_stats", "Reset stats"),
        Binding("t", "toggle_follow", "Follow"),
        Binding("d", "toggle_debug", "Debug view"),
        Binding("slash", "focus_search", "Search"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        log_paths: Sequence[str] | None = None,
        *,
        buffer_size: int = 50_000,
        start_at_end: bool = True,
        export_dir: str | Path = ".",
        filter_spec: FilterSpec | None = None,
        offline_diagnostics: bool = False,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.log_paths = [str(p) for p in (log_paths or [])]
        self._start_at_end = start_at_end
        self._export_dir = Path(export_dir).expanduser()

        self.stream = LogStream(
            self.log_paths,
            buffer_size=buffer_size,
            start_at_end=start_at_end,
            encoding=encoding,
            on_error=lambda exc, path: self._post_error(exc, path),
            on_rotate=lambda path: self._post_note(f"rotated: {path}"),
        )
        self.stats = LogStats()
        self.filters = FilterEngine(filter_spec)
        self.engine = DiagnosticEngine(offline=offline_diagnostics)
        self.exporter = Exporter()

        self._follow = True
        self._debug_view = False
        self._paused = False
        self._rendered = 0            # how many buffer entries already drawn
        self._displayed = 0           # how many lines the view currently shows
        self._pending: list[LogEntry] = []
        self._last_error: LogEntry | None = None
        self._diag_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._last_snapshot = StatsSnapshot()
        self._spec_description = self.filters.spec.describe()
        self._messages: list[str] = []
        self._diagnose_seconds = 0.0
        self._started_at = time.time()

    # -- layout ----------------------------------------------------------- #

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield RichLog(id="logview", highlight=False, markup=False,
                          wrap=True, max_lines=MAX_RENDERED_LINES, auto_scroll=True)
            with Vertical(id="side"):
                yield Sparkline(id="sparkline")
                yield StatusPanel(id="status")
                yield FilterPanel(id="activefilter")
                with TabbedContent(id="tabs"):
                    with TabPane("diagnose", id="tab_diag"), \
                            VerticalScroll(id="diagscroll"):
                        yield DiagnosisPanel(id="diagpane")
                    with TabPane("signatures", id="tab_sig"):
                        yield DataTable(id="sigtable", zebra_stripes=True)
                    with TabPane("files", id="tab_files"):
                        yield DataTable(id="filetable", zebra_stripes=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "SmartLog"
        self.sub_title = f"{len(self.log_paths)} source(s)"
        self._prepare_tables()

        if not self.log_paths:
            self.notify("no log files given - run with paths or --demo", severity="error")
            return

        await self.stream.start()
        self._pump_task = asyncio.create_task(self._pump())
        self._tick_task = asyncio.create_task(self._tick())
        self._prime_view()

    def _prepare_tables(self) -> None:
        with contextlib.suppress(Exception):
            sig = self.query_one("#sigtable", DataTable)
            sig.add_columns("signature", "count")
            files = self.query_one("#filetable", DataTable)
            files.add_columns("file", "state")

    def _prime_view(self) -> None:
        """Draw whatever is already buffered (used when tailing from the start)."""
        entries = self.stream.snapshot()
        if entries:
            self.stats.record_many(entries)
            self._render_entries(entries, force_all=True)
            self._rendered = len(entries)

    # -- ingestion -------------------------------------------------------- #

    async def _pump(self) -> None:
        """Drain reader batches into the stats engine and the pending view list.

        ``pause`` freezes the *view*, not ingestion: statistics keep updating and
        nothing is lost, it just is not rendered until you resume.
        """
        try:
            async for batch in self.stream.batches(timeout=0.5):
                self.stats.record_many(batch)
                if self._paused:
                    continue
                self._pending.extend(batch)
                if len(self._pending) > 20_000:
                    # Bound the backlog: shed the oldest pending entries and
                    # report it instead of silently losing data.
                    excess = len(self._pending) - 20_000
                    del self._pending[:excess]
                    self.stats.drop(excess)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._post_error(exc, Path("."))

    async def _tick(self) -> None:
        """Render pass. Runs at UI_TICK so rendering cost is bounded per frame."""
        while True:
            try:
                snap = self.stats.snapshot()
                self._last_snapshot = snap
                if self._pending:
                    self._render_entries(self._pending)
                    self._pending.clear()
                self._refresh_panels(snap)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                self._post_error(exc, Path("."))
            await asyncio.sleep(UI_TICK)

    # -- rendering -------------------------------------------------------- #

    def _render_entries(self, entries: Iterable[LogEntry], *, force_all: bool = False) -> None:
        view = self.query_one("#logview", RichLog)
        written = 0
        for entry in entries:
            if self.filters.matches(entry):
                view.write(self._format_entry(entry))
                written += 1
        if written and self._follow:
            view.scroll_end(animate=False)
        # ``RichLog`` has no stable public line-count API across versions, so the
        # app tracks what it has drawn itself.
        self._rendered += 1 if force_all else written
        self._displayed += written

    @property
    def displayed_lines(self) -> int:
        """Number of lines currently rendered in the log view."""
        return self._displayed

    def _format_entry(self, entry: LogEntry) -> Text:
        text = Text(no_wrap=False)
        stamp = entry.timestamp or datetime.fromtimestamp(
            entry.ts_epoch or time.time()
        ).strftime("%H:%M:%S")
        text.append(stamp, style="dim")
        text.append(" ")
        text.append(f"{entry.level.label:<8}", style=LEVEL_COLORS.get(entry.level, "white"))
        if entry.source:
            text.append(f" {Path(entry.source).name:<16}", style="magenta dim")
        text.append(" ")

        body = entry.raw if self._debug_view else (entry.message or entry.raw)
        start = len(text)
        text.append(body, style="red" if entry.is_error else "default")

        # Highlight terms are applied to the rendered span only, so the offsets
        # stay correct regardless of the prefix widths above.
        terms = self.filters.spec.search
        if terms:
            for begin, end in highlight_terms(body, terms):
                text.stylize(f"bold {HIGHLIGHT_STYLE}", start + begin, start + end)

        if entry.level >= Level.ERROR:
            self._last_error = entry
        return text

    def _refresh_panels(self, snap: StatsSnapshot) -> None:
        hidden = self._visible_count()
        with contextlib.suppress(Exception):
            self.query_one("#status", StatusPanel).refresh_stats(
                snap, f"{self._displayed:,} shown / {hidden:,} filtered"
            )
        with contextlib.suppress(Exception):
            self.query_one("#sparkline", Sparkline).refresh_spark(
                snap.sparkline, "rate"
            )
        with contextlib.suppress(Exception):
            self.query_one("#activefilter", FilterPanel).refresh_filter(
                self._spec_description
            )
        self._refresh_signature_table(snap)
        self._refresh_file_table()

    def _refresh_signature_table(self, snap: StatsSnapshot) -> None:
        table = self._value("#sigtable", DataTable)
        if table is None:
            return
        if table.row_count != len(snap.top_fingerprints):
            table.clear()
            table.add_columns("signature", "count")
            for name, count in snap.top_fingerprints:
                table.add_row(name, f"{count:,}")
        else:
            for index, (_, count) in enumerate(snap.top_fingerprints):
                with contextlib.suppress(Exception):
                    table.update_cell((index, 1), f"{count:,}")

    def _refresh_file_table(self) -> None:
        health = self.stream.worker_health()
        table = self._value("#filetable", DataTable)
        if table is None:
            return
        if table.row_count != len(health):
            table.clear()
            table.add_columns("file", "state")
            for path, _, _ in health:
                table.add_row(Path(path).name, "")
        for index, (path, alive, state) in enumerate(health):
            with contextlib.suppress(Exception):
                colour = "green" if alive else "red"
                table.update_cell((index, 0), Path(path).name)
                table.update_cell((index, 1), f"[{colour}]{state}[/{colour}]")

    def _visible_count(self) -> int:
        """Entries currently held but not yet rendered (i.e. filtered out)."""
        spec = self.filters.spec
        if spec.is_empty():
            return 0
        return sum(1 for e in self._pending if not self.filters.matches(e))

    def _value(self, selector: str, cls: type) -> Any:
        with contextlib.suppress(Exception):
            return self.query_one(selector, cls)
        return None

    # -- notifications ---------------------------------------------------- #

    def _post_error(self, exc: Exception, path: Path) -> None:
        message = f"{type(exc).__name__}: {exc}"
        self._messages.append(message)
        with contextlib.suppress(Exception):
            self.notify(message[:120], severity="error", timeout=6)

    def _post_note(self, message: str) -> None:
        self._messages.append(message)
        with contextlib.suppress(Exception):
            self.notify(message, severity="warning", timeout=4)

    # -- actions ---------------------------------------------------------- #

    def action_pause(self) -> None:
        """Freeze the view. Ingestion and statistics keep running.

        On pause the unrendered backlog is dropped so the view really stops
        moving; on resume the tail of the ring buffer is re-rendered so nothing
        is silently lost.
        """
        self._paused = not self._paused
        if self._paused:
            withheld = len(self._pending)
            self._pending.clear()
            self.notify(
                f"paused — view frozen, {withheld:,} unrendered lines withheld"
                if withheld else "paused — view frozen, reading continues",
                severity="warning",
            )
            return
        backlog = self.stream.tail(RESUME_BACKFILL)
        self._pending.extend(backlog)
        self.notify(f"resumed — replayed {len(backlog):,} buffered lines",
                    severity="information")

    def action_clear(self) -> None:
        view = self._value("#logview", RichLog)
        if view is not None:
            view.clear()
        self.stream.clear()
        self._pending.clear()
        self._rendered = 0
        self._displayed = 0
        self.notify("view cleared", severity="information")

    def action_reset_stats(self) -> None:
        self.stats.reset()
        self.notify("statistics reset", severity="information")

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        view = self._value("#logview", RichLog)
        if view is not None:
            view.auto_scroll = self._follow
        self.notify(f"follow {'on' if self._follow else 'off'}", severity="information")

    def action_toggle_debug(self) -> None:
        self._debug_view = not self._debug_view
        self.notify(f"debug view {'on' if self._debug_view else 'off'}", severity="information")

    def action_focus_search(self) -> None:
        """Open the filter dialog focused on the highlight field."""
        self.push_screen(FilterDialog(focus="search"), self._apply_filter_result)

    def action_help(self) -> None:
        panel = self._value("#diagpane", DiagnosisPanel)
        if panel is not None:
            panel.show_idle(
                "a diagnose · f filter · e export · p pause · c clear · "
                "r reset stats · t follow · d debug view · q quit"
            )

    def action_filter(self) -> None:
        self.push_screen(FilterDialog(), self._apply_filter_result)

    def _apply_filter_result(self, result: dict[str, Any] | None) -> None:
        """Handle the FilterDialog callback."""
        if not result:
            return
        if result.get("__reset__"):
            self.filters.set_spec(build_spec())
            self._spec_description = self.filters.spec.describe()
            self.notify("filters cleared", severity="information")
            return
        try:
            spec = build_spec(
                min_level=result.get("min_level"),
                sources=[s.strip() for s in (result.get("sources") or "").split(",")
                         if s.strip()],
                include=result.get("include"),
                exclude=result.get("exclude"),
                exclude_regex=[s.strip() for s
                               in (result.get("exclude_regex") or "").split(",")
                               if s.strip()],
                search=result.get("search") or "",
                error_only=bool(result.get("error_only")),
            )
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return
        self.filters.set_spec(spec)
        self._spec_description = spec.describe()
        self.notify(f"filter: {self._spec_description}",
                    severity="information", timeout=8)

    def action_export(self) -> None:
        entries = self._entries_for_export()
        if not entries:
            self.notify("nothing to export — no entries match the view", severity="warning")
            return

        def finish(result: dict[str, Any] | None) -> None:
            if not result:
                return
            if result["mode"] == "copy":
                self._copy_export(entries, result["fmt"])
            else:
                self._save_export(entries, result["fmt"], result["dest"])

        self.push_screen(
            ExportDialog(total=len(entries), filters=self._spec_description,
                         default_dir=self._export_dir),
            finish,
        )

    def _save_export(self, entries: list[LogEntry], fmt: str, dest: str) -> None:
        try:
            path = self.exporter.write(
                entries, fmt, dest,
                sources=self.log_paths,
                filters=self._spec_description,
                stats=self._last_snapshot,
            )
        except (OSError, ValueError) as exc:
            self.notify(f"export failed: {exc}", severity="error")
            return
        self.notify(f"wrote {path} ({len(entries):,} entries)",
                    severity="information", timeout=8)

    def _copy_export(self, entries: list[LogEntry], fmt: str) -> None:
        try:
            ok, message = self.exporter.copy(
                entries, fmt,
                sources=self.log_paths,
                filters=self._spec_description,
                stats=self._last_snapshot,
            )
        except (ValueError, OSError) as exc:
            self.notify(f"copy failed: {exc}", severity="error")
            return
        self.notify(message if ok else f"copy failed: {message}",
                    severity="information" if ok else "error", timeout=8)

    def _entries_for_export(self) -> list[LogEntry]:
        buffer = self.stream.snapshot()
        return self.filters.apply(buffer)

    # -- diagnosis -------------------------------------------------------- #

    def action_diagnose(self) -> None:
        if self._diag_task is not None and not self._diag_task.done():
            self.notify("a diagnosis is already running", severity="warning")
            return

        entry = self._pick_error()
        if entry is None:
            self.notify("no error entry available to diagnose", severity="warning")
            return

        panel = self._value("#diagpane", DiagnosisPanel)
        if panel is not None:
            panel.show_busy(entry.message[:120] or entry.raw[:120])
        buffer = self.stream.snapshot()
        self._diag_task = asyncio.create_task(self._diagnose(entry, buffer))

    def _pick_error(self) -> LogEntry | None:
        """Prefer the selected row, else the most recent error in view."""
        pending = [e for e in self._pending if e.is_error]
        pool = pending or self.stream.tail(400)
        errors = [e for e in pool if e.is_error]
        return errors[-1] if errors else None

    async def _diagnose(self, entry: LogEntry, buffer: list[LogEntry]) -> None:
        started = time.perf_counter()
        try:
            diagnosis = await self.engine.diagnose_entry(entry, buffer)
        except Exception as exc:
            self._messages.append(f"diagnosis failed: {exc!r}")
            panel = self._value("#diagpane", DiagnosisPanel)
            if panel is not None:
                panel.show_idle(f"diagnosis failed: {exc}")
            return
        panel = self._value("#diagpane", DiagnosisPanel)
        if panel is not None:
            panel.show(diagnosis)
        self._diagnose_seconds = time.perf_counter() - started
        if diagnosis.source != "offline":
            self.notify(
                f"diagnosed by {diagnosis.source} in "
                f"{self._diagnose_seconds:.1f}s "
                f"(no UI freeze — the loop stayed responsive)",
                severity="information", timeout=6,
            )

    # -- teardown --------------------------------------------------------- #

    async def action_quit(self) -> None:
        for task in (self._pump_task, self._tick_task, self._diag_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self.stream.stop()
        self.exit()


def run_tui(
    log_paths: Sequence[str],
    *,
    buffer_size: int = 50_000,
    start_at_end: bool = True,
    export_dir: str | Path = ".",
) -> int:
    """Launch the TUI. Returns a process exit code."""
    app = SmartLogApp(log_paths, buffer_size=buffer_size,
                      start_at_end=start_at_end, export_dir=export_dir)
    app.run()
    return 0