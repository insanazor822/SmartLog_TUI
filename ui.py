"""
Textual TUI Interface for SmartLog TUI
Provides the main user interface with panels, controls, and keyboard shortcuts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
)

# Import project modules
from ai_diagnostics import AIDiagnosticEngine, Diagnosis
from exporter import LogExporter
from license import LicenseTier, get_license_manager
from log_reader import LogEntry, LogStreamManager
from parser import AnomalyStats, FilterEngine, LogLevel, LogParser, LogStatistics

# Color coding for log levels
LOG_LEVEL_COLORS = {
    'DEBUG': 'dim white',
    'INFO': 'green',
    'WARN': 'yellow',
    'WARNING': 'yellow',
    'ERROR': 'red',
    'CRITICAL': 'bold red',
    'FATAL': 'bold red on black',
}


class StatisticsPanel(Static):
    """Panel for displaying error statistics."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stats: LogStatistics | None = None
        self._anomaly_stats: AnomalyStats | None = None

    def update_statistics(
        self,
        stats: LogStatistics,
        anomaly_stats: AnomalyStats,
    ) -> None:
        """Update the displayed statistics."""
        self._stats = stats
        self._anomaly_stats = anomaly_stats
        self.refresh()

    def render(self) -> str:
        """Render the statistics panel."""
        if not self._stats or not self._anomaly_stats:
            return "[dim]No statistics data yet...[/dim]\n\nPress 'q' to quit."

        output = []
        output.append("[bold cyan]=== STATISTICS ===[/bold cyan]")
        output.append(f"[green]Total Lines:[/green] {self._stats.total_lines:,}")
        output.append(f"[green]Throughput:[/green] {self._stats.lines_per_second:.1f} lines/sec\n")

        # Error stats
        output.append("[bold yellow]=== ANOMALIES & ERRORS ===[/bold yellow]")
        output.append(f"[red]Total Errors:[/red] {self._anomaly_stats.total_errors:,}")
        output.append(f"[red]Errors (Last 1m):[/red] {self._anomaly_stats.errors_last_minute}")
        output.append(f"[red]Error Rate:[/red] {self._anomaly_stats.error_rate_per_minute:.1f}/min")

        if self._anomaly_stats.is_spike:
            output.append("\n[blink bold red]⚠ ERROR SPIKE DETECTED![/blink bold red]")
        output.append("")

        # Errors by level
        if self._anomaly_stats.errors_by_level:
            output.append("[yellow]By Level:[/yellow]")
            for level, count in sorted(self._anomaly_stats.errors_by_level.items()):
                color = LOG_LEVEL_COLORS.get(level, 'white')
                output.append(f"  [{color}]{level}: {count}[/]")

        # Errors by category
        if self._anomaly_stats.errors_by_category:
            output.append("\n[yellow]By Category:[/yellow]")
            for category, count in sorted(self._anomaly_stats.errors_by_category.items()):
                output.append(f"  [cyan]{category}:[/cyan] {count}")

        return '\n'.join(output)


class AIDiagnosisPanel(Static):
    """Panel for displaying AI diagnosis results."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._diagnosis: Diagnosis | None = None
        self._license_message: str | None = None
        self._engine = AIDiagnosticEngine()
        self._license_manager = get_license_manager()

    def diagnose_error(self, error_entry: LogEntry, all_entries: list[LogEntry]) -> None:
        """Perform diagnosis on an error entry."""
        usage_result = self._license_manager.use_ai_diagnosis()
        self._license_message = usage_result.get('message', '')

        if not usage_result.get('allowed', False):
            self._diagnosis = None
            self.refresh()
            return

        try:
            self._diagnosis = self._engine.diagnose_from_logs(all_entries, error_entry)
        except Exception as e:
            self._diagnosis = None
            self._license_message = f"Diagnosis error: {e}"

        self.refresh()

    def clear_diagnosis(self) -> None:
        """Clear the current diagnosis."""
        self._diagnosis = None
        self._license_message = None
        self.refresh()

    def render(self) -> str:
        """Render the AI diagnosis panel."""
        output = []
        output.append("[bold blue]=== AI DIAGNOSIS ===[/bold blue]\n")

        if self._license_message:
            output.append(f"[yellow]{self._license_message}[/yellow]\n")

        if not self._diagnosis:
            output.append("[dim]No active diagnosis.[/dim]")
            output.append("Select an [red]ERROR[/red] log and press [bold cyan]'a'[/bold cyan] to diagnose.")
        else:
            output.append(f"[bold red]Summary:[/bold red] {self._diagnosis.error_summary}")

            conf_val = getattr(self._diagnosis.confidence, 'value', str(self._diagnosis.confidence))
            output.append(f"[green]Confidence:[/green] {str(conf_val).upper()}")
            output.append(f"[yellow]Root Cause:[/yellow] {self._diagnosis.root_cause}\n")

            if hasattr(self._diagnosis, 'recommendations') and self._diagnosis.recommendations:
                output.append("[bold cyan]Recommendations:[/bold cyan]")
                for i, rec in enumerate(self._diagnosis.recommendations[:3], 1):
                    title = getattr(rec, 'title', 'Recommendation')
                    desc = getattr(rec, 'description', '')
                    cmd = getattr(rec, 'command', None)
                    output.append(f"  {i}. [bold]{title}[/bold]")
                    output.append(f"     {desc}")
                    if cmd:
                        output.append(f"     [dim]$ {cmd}[/dim]")
                    output.append("")

        return '\n'.join(output)


class LicenseScreen(ModalScreen[dict[str, Any] | None]):
    """Modal screen for license management."""

    BINDINGS = [
        Binding('escape', 'cancel', 'Close'),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._license_manager = get_license_manager()

    def compose(self) -> ComposeResult:
        with Vertical(id="modal_dialog"):
            yield Static("[bold blue]=== LICENSE MANAGEMENT ===[/bold blue]\n")
            yield Static("Enter your SmartLog license key below:")
            yield Input(placeholder="SL-PRO-XXXX-XXXX-XXXX", id="license_input")
            yield Label("", id="license_status")
            with Horizontal(classes="button_row"):
                yield Button("Activate", variant="primary", id="activate_btn")
                yield Button("Cancel", variant="default", id="cancel_btn")
            yield Static("\n[green]Demo Key:[/green] SL-DEMO-2024-PRO-UNLIMITED", classes="hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks."""
        if event.button.id == "activate_btn":
            self.activate_license()
        elif event.button.id == "cancel_btn":
            self.dismiss(None)

    def activate_license(self) -> None:
        """Activate the entered license."""
        input_widget = self.query_one("#license_input", Input)
        status_label = self.query_one("#license_status", Label)
        license_key = input_widget.value.strip()

        if not license_key:
            status_label.update("[red]Please enter a license key.[/red]")
            return

        result = self._license_manager.activate_license(license_key)

        if result.get('success'):
            self.dismiss(result.get('license'))
        else:
            status_label.update(f"[red]{result.get('error', 'Activation failed.')}[/red]")

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilterScreen(ModalScreen[dict[str, Any] | None]):
    """Modal screen for setting filters."""

    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel'),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal_dialog"):
            yield Static("[bold blue]=== SET LOG FILTERS ===[/bold blue]\n")
            yield Static("Text Filter (Contains):")
            yield Input(placeholder="Enter search keyword...", id="text_filter")
            yield Static("\nMinimum Log Level:")
            yield Static("  [dim]DEBUG | INFO | WARN | ERROR | CRITICAL[/dim]")
            yield Input(placeholder="e.g. WARN or ERROR", id="level_filter")
            with Horizontal(classes="button_row"):
                yield Button("Apply", variant="primary", id="apply_btn")
                yield Button("Clear Filters", variant="warning", id="clear_btn")
                yield Button("Cancel", variant="default", id="cancel_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply_btn":
            self.apply_filters()
        elif event.button.id == "clear_btn":
            self.dismiss({'text': None, 'level': None})
        elif event.button.id == "cancel_btn":
            self.dismiss(None)

    def apply_filters(self) -> None:
        """Apply the entered filters."""
        text_input = self.query_one("#text_filter", Input)
        level_input = self.query_one("#level_filter", Input)

        text_filter = text_input.value.strip() or None
        level_str = level_input.value.strip().upper()

        level_filter = None
        if level_str:
            if hasattr(LogLevel, level_str):
                level_filter = getattr(LogLevel, level_str)
            elif level_str in LogLevel.__members__:
                level_filter = LogLevel[level_str]

        self.dismiss({
            'text': text_filter,
            'level': level_filter,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


class ExportScreen(ModalScreen[dict[str, Any] | None]):
    """Modal screen for exporting the currently viewed / filtered logs."""

    BINDINGS = [
        Binding('escape', 'cancel', 'Cancel'),
    ]

    SUPPORTED_FORMATS = ('md', 'json', 'txt')

    def __init__(
        self,
        exporter: LogExporter,
        entries: list[LogEntry],
        text_filter: str | None = None,
        min_level: LogLevel | None = None,
    ) -> None:
        super().__init__()
        self._exporter = exporter
        self._entries = entries
        self._text_filter = text_filter
        self._min_level = min_level

    def compose(self) -> ComposeResult:
        with Vertical(id="modal_dialog"):
            yield Static("[bold blue]=== EXPORT & SHARE ===[/bold blue]\n")
            yield Static(
                f"Exporting [bold green]{len(self._entries)}[/bold green] log entries (current view + active filters)."
            )
            yield Static(f"Active filters: {self._filter_description()}\n")
            yield Static("Export Format:")
            with RadioSet(id="format_radio"):
                yield RadioButton("Markdown (.md)", value=True, id="md")
                yield RadioButton("JSON (.json)", id="json")
                yield RadioButton("Plain Text (.txt)", id="txt")

            yield Static("\nOutput Path (file export):")
            yield Input(value=self._exporter.default_filename("md"), id="export_path")
            yield Static("", id="export_status")
            with Horizontal(classes="button_row"):
                yield Button("Save File", variant="primary", id="save_btn")
                yield Button("Copy to Clipboard", variant="success", id="copy_btn")
                yield Button("Cancel", variant="default", id="cancel_btn")

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Update the default file path when format selection changes."""
        fmt = event.pressed.id or "md"
        try:
            path_widget = self.query_one("#export_path", Input)
            path_widget.value = self._exporter.default_filename(fmt)
        except NoMatches:
            pass

    def _filter_description(self) -> str:
        parts = []
        if self._text_filter:
            parts.append(f"contains '{self._text_filter}'")
        if self._min_level:
            parts.append(f"level >= {getattr(self._min_level, 'value', self._min_level)}")
        return "; ".join(parts) if parts else "none (all logs)"

    def _selected_format(self) -> str:
        try:
            radio_set = self.query_one("#format_radio", RadioSet)
            if radio_set.pressed_button and radio_set.pressed_button.id in self.SUPPORTED_FORMATS:
                return radio_set.pressed_button.id
        except NoMatches:
            pass
        return "md"

    def _set_status(self, markup: str) -> None:
        try:
            status = self.query_one("#export_status", Static)
            status.update(markup)
        except NoMatches:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button clicks."""
        if event.button.id == "save_btn":
            self._save_to_file()
        elif event.button.id == "copy_btn":
            self._copy_clipboard()
        elif event.button.id == "cancel_btn":
            self.dismiss(None)

    def _save_to_file() -> None:
        pass

    def _save_to_file(self) -> None:
        """Save the export to the selected file path."""
        fmt = self._selected_format()
        path_widget = self.query_one("#export_path", Input)
        path_str = path_widget.value.strip() or self._exporter.default_filename(fmt)

        suffix_map = {".md": "md", ".json": "json", ".txt": "txt"}
        if suffix_map.get(Path(path_str).suffix.lower()) != fmt:
            path_str = str(Path(path_str).with_suffix("." + fmt))
        path_str = str(Path(path_str).expanduser())

        try:
            ok, message = self._exporter.export_to_file(
                self._entries,
                fmt,
                Path(path_str),
                text_filter=self._text_filter,
                min_level=self._min_level,
            )
        except Exception as exc:
            ok, message = False, str(exc)

        if ok:
            self.dismiss({
                "ok": True,
                "message": f"Exported {len(self._entries)} logs to {message}",
            })
        else:
            self._set_status(f"[red]{message}[/red]")

    def _copy_clipboard(self) -> None:
        """Copy the export to the system clipboard."""
        fmt = self._selected_format()
        try:
            ok, message = self._exporter.copy_to_clipboard(
                self._entries,
                fmt,
                text_filter=self._text_filter,
                min_level=self._min_level,
            )
        except Exception as exc:
            ok, message = False, str(exc)

        if ok:
            self.dismiss({
                "ok": True,
                "message": f"Copied {len(self._entries)} logs ({fmt.upper()}) to clipboard",
            })
        else:
            self._set_status(f"[red]{message}[/red]")

    def action_cancel(self) -> None:
        self.dismiss(None)


class SmartLogApp(App[None]):
    """Main SmartLog TUI Application."""

    CSS = """
    Screen {
        background: $surface;
    }

    Header {
        dock: top;
        background: $primary;
        color: $text;
    }

    Footer {
        dock: bottom;
        background: $surface;
        color: $text;
    }

    #main_container {
        layout: grid;
        grid-size: 2 2;
        grid-rows: 2fr 1fr;
        grid-columns: 1fr 1fr;
        height: 100%;
        padding: 1;
    }

    #log_panel {
        column-span: 2;
        border: solid $primary;
        height: 100%;
        background: $panel;
    }

    #stats_panel {
        border: solid $secondary;
        height: 100%;
        padding: 1;
    }

    #diagnosis_panel {
        border: solid $accent;
        height: 100%;
        padding: 1;
    }

    #modal_dialog {
        padding: 2;
        background: $panel;
        border: heavy $primary;
        width: 60;
        height: auto;
        align: center middle;
    }

    .button_row {
        margin-top: 1;
        height: auto;
        align: center middle;
    }

    .hint {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding('q', 'quit_app', 'Quit', priority=True),
        Binding('p', 'toggle_pause', 'Pause/Resume'),
        Binding('c', 'clear_logs', 'Clear Logs'),
        Binding('f', 'set_filter', 'Filter'),
        Binding('a', 'diagnose', 'AI Diagnose'),
        Binding('l', 'license', 'License'),
        Binding('r', 'refresh_stats', 'Reset Stats'),
        Binding('e', 'export_logs', 'Export'),
    ]

    def __init__(self, log_paths: list[str] | None = None) -> None:
        super().__init__()
        self.log_paths = log_paths or [
            '/var/log/syslog',
            '/var/log/nginx/error.log',
            '/var/log/app.log',
        ]

        # Core engines
        self.log_manager = LogStreamManager()
        self.parser = LogParser()
        self.filter_engine = FilterEngine(self.parser)
        self.license_manager = get_license_manager()

        # Export & Share
        self.exporter = LogExporter(source_paths=self.log_paths)
        self._active_text_filter: str | None = None
        self._active_min_level: LogLevel | None = None

        # State
        self._paused = False
        self._last_error_entry: LogEntry | None = None
        self._stats_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        """Compose the UI."""
        yield Header()
        with Container(id="main_container"):
            yield RichLog(id="log_panel", highlight=True, markup=True, wrap=True)
            yield StatisticsPanel(id="stats_panel")
            yield AIDiagnosisPanel(id="diagnosis_panel")
        yield Footer()

    async def on_mount(self) -> None:
        """Mount and start background logging tasks."""
        self.title = "SmartLog TUI"
        self.sub_title = "Real-time Log Analysis & AI Diagnostics"

        buffer_limit = self.license_manager.get_log_buffer_limit()
        await self.log_manager.initialize(self.log_paths, max_buffer_size=buffer_limit)
        self.log_manager.add_callback(self._on_new_log_entry)

        await self.log_manager.start()
        self._stats_task = asyncio.create_task(self._update_statistics_loop())

    def _on_new_log_entry(self, entry: LogEntry) -> None:
        """Handle incoming log stream lines and route safely to RichLog."""
        # 1. Update stats & parser
        self.parser.update_statistics(entry.raw, entry.source)

        # 2. Track last error for AI diagnosis
        analysis = self.parser.analyze_line(entry.raw)
        if analysis.get('is_error'):
            self._last_error_entry = entry

        # 3. Apply active filters
        if not self.filter_engine.matches(entry):
            return

        # 4. Render safely to RichLog widget
        try:
            log_widget = self.query_one("#log_panel", RichLog)
            level: LogLevel = analysis.get('level', LogLevel.INFO)
            level_str = getattr(level, 'value', str(level))
            color = LOG_LEVEL_COLORS.get(level_str, 'white')

            timestamp = entry.timestamp or datetime.now().strftime("%H:%M:%S")
            level_tag = f"[{color}][{level_str:7}][/{color}]"
            source_tag = f"[dim]({entry.source})[/dim]" if entry.source else ""

            # Escape raw log content so brackets or special chars don't break Rich markup
            safe_raw = escape(entry.raw)

            log_widget.write(f"[dim]{timestamp}[/] {level_tag} {source_tag} {safe_raw}")
        except NoMatches:
            pass

    async def _update_statistics_loop(self) -> None:
        """Background loop refreshing statistics panel."""
        while True:
            try:
                stats_panel = self.query_one("#stats_panel", StatisticsPanel)
                stats_panel.update_statistics(
                    self.parser.stats,
                    self.parser.anomaly_stats,
                )
            except NoMatches:
                pass
            except asyncio.CancelledError:
                break
            await asyncio.sleep(1.0)

    async def action_quit_app(self) -> None:
        """Gracefully terminate background workers and exit."""
        if self._stats_task:
            self._stats_task.cancel()
        await self.log_manager.stop()
        self.exit()

    def action_toggle_pause(self) -> None:
        """Toggle log stream tailing."""
        if self._paused:
            self.log_manager.resume()
            self._paused = False
            self.notify("Log streaming resumed", severity="information")
        else:
            self.log_manager.pause()
            self._paused = True
            self.notify("Log streaming paused", severity="warning")

    def action_clear_logs(self) -> None:
        """Clear the visual log widget and memory buffer."""
        self.log_manager.clear_buffer()
        try:
            log_widget = self.query_one("#log_panel", RichLog)
            log_widget.clear()
            self.notify("Logs cleared", severity="information")
        except NoMatches:
            pass

    def action_set_filter(self) -> None:
        """Open filter settings modal."""
        self.push_screen(FilterScreen(), self._on_filter_applied)

    def _on_filter_applied(self, filters: dict[str, Any] | None) -> None:
        """Apply filters returned by modal."""
        if filters is not None:
            self.filter_engine.set_text_filter(filters.get('text'))
            self.filter_engine.set_level_filter(filters.get('level'))

            # Track active filters for Export metadata
            self._active_text_filter = filters.get('text')
            self._active_min_level = filters.get('level')

            info = []
            if filters.get('text'):
                info.append(f"Text: '{filters['text']}'")
            if filters.get('level'):
                lvl = filters['level']
                info.append(f"Min Level: {getattr(lvl, 'value', lvl)}")

            msg = " | ".join(info) if info else "All filters cleared"
            self.notify(f"Filter updated: {msg}", severity="information")

    def _get_viewed_entries(self) -> list[LogEntry]:
        """Return the log entries currently matching active filters."""
        entries = self.log_manager.get_buffer()
        try:
            return [entry for entry in entries if self.filter_engine.matches(entry)]
        except Exception:
            return list(entries)

    def action_export_logs(self) -> None:
        """Open the Export & Share modal (binding: 'e')."""
        entries = self._get_viewed_entries()
        if not entries:
            self.notify("Nothing to export - no logs match the current view.", severity="warning")
            return

        screen = ExportScreen(
            exporter=self.exporter,
            entries=entries,
            text_filter=self._active_text_filter,
            min_level=self._active_min_level,
        )
        self.push_screen(screen, self._on_export_result)

    def _on_export_result(self, result: dict[str, Any] | None) -> None:
        """Handle the result returned by the Export & Share modal."""
        if not result:
            return
        if result.get('ok'):
            self.notify(result.get('message', 'Export completed'), severity="information")
        else:
            self.notify(result.get('message', 'Export failed'), severity="error")

    def action_diagnose(self) -> None:
        """Perform AI diagnosis on the most recent error."""
        if not self._last_error_entry:
            self.notify("No error found in stream to diagnose.", severity="warning")
            return

        if not self.license_manager.can_use_ai_diagnosis():
            self.notify("License limit reached. Upgrade to Pro.", severity="error")
            return

        try:
            diagnosis_panel = self.query_one("#diagnosis_panel", AIDiagnosisPanel)
            all_entries = self.log_manager.get_buffer()
            diagnosis_panel.diagnose_error(self._last_error_entry, all_entries)
            self.notify("AI diagnosis completed", severity="information")
        except NoMatches:
            pass

    def action_license(self) -> None:
        """Open license management modal."""
        self.push_screen(LicenseScreen(), self._on_license_activated)

    def _on_license_activated(self, license_info: dict[str, Any] | None) -> None:
        """Handle license change."""
        if license_info:
            tier = license_info.get('tier', 'free')
            self.notify(f"License Activated: {str(tier).upper()} Tier", severity="information")

    def action_refresh_stats(self) -> None:
        """Reset internal metrics and anomaly rates."""
        self.parser.reset_statistics()
        self.notify("Statistics and anomaly metrics reset", severity="information")


def main() -> None:
    """Application CLI entry point."""
    import sys

    log_paths = [arg for arg in sys.argv[1:] if not arg.startswith('-')]
    app = SmartLogApp(log_paths if log_paths else None)
    app.run()


if __name__ == "__main__":
    main()
