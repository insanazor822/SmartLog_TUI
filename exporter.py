"""
Export & Share module for SmartLog TUI.

Central module used by the TUI (ui.py) and the CLI (main.py) to export the
currently viewed / filtered logs into shareable formats:

    * Markdown   (.md)
    * JSON       (.json)
    * Plain Text (.txt)

Every export contains a clean header / metadata section with:
    - total log count
    - covered date range (min -> max timestamp)
    - active filters (text + minimum level)
    - monitored source files
    - per-level summary

Exports can also be copied directly to the system clipboard
(Windows: clip.exe | macOS: pbcopy | Linux: wl-copy / xclip / xsel,
with a pyperclip fallback).

The module is dependency-light (standard library only) and safe to import
from both the Textual app and plain script contexts.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from log_reader import LogEntry, LogStreamReader

APP_NAME = "SmartLog TUI"
APP_VERSION = "1.0.0"
SUPPORTED_FORMATS: tuple[str, ...] = ("md", "json", "txt")


# ---------------------------------------------------------------------------
# System clipboard helpers (cross-platform, no hard third-party dependency)
# ---------------------------------------------------------------------------

class SystemClipboard:
    """Cross-platform copy-to-clipboard helper."""

    @classmethod
    def copy(cls, text: str) -> tuple[bool, str]:
        """Copy text to the system clipboard. Returns (success, message)."""
        data = text.encode("utf-8")
        system = platform.system()

        if system == "Windows":
            return cls._run(["clip.exe"], data, "clip.exe")
        if system == "Darwin":
            return cls._run(["pbcopy"], data, "pbcopy")

        # Linux / BSD: try Wayland, then X11 tooling sequentially
        last_error = ""
        for command in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if shutil.which(command[0]):
                ok, msg = cls._run(command, data, command[0])
                if ok:
                    return True, msg
                last_error = msg

        # Last resort: optional pyperclip package
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(text)
            return True, "Copied to clipboard via pyperclip."
        except Exception as exc:
            err_details = f" ({last_error})" if last_error else f" ({exc})"
            return False, (
                f"Could not access the system clipboard{err_details}. "
                "Install xclip, xsel or wl-clipboard (or pyperclip) to enable copying."
            )

    @staticmethod
    def _run(command: list[str], data: bytes, label: str) -> tuple[bool, str]:
        try:
            subprocess.run(
                command,
                input=data,
                check=True,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"Copied to clipboard via {label}."
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"Clipboard copy via {label} failed: {exc}"


# ---------------------------------------------------------------------------
# Log exporter
# ---------------------------------------------------------------------------

class LogExporter:
    """
    Renders SmartLog log buffers to Markdown / JSON / Plain Text and writes
    them to disk or the system clipboard.

    Usage:
        exporter = LogExporter(source_paths=["/var/log/syslog"])
        content = exporter.render(entries, "md", text_filter="error", min_level=LogLevel.ERROR)
        ok, msg = exporter.export_to_file(entries, "md", Path("out.md"))
        ok, msg = exporter.copy_to_clipboard(entries, "json")
    """

    def __init__(self, source_paths: list[str] | None = None) -> None:
        self.source_paths: list[str] = list(source_paths or [])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def normalize_format(cls, fmt: str) -> str:
        """Map user-facing format names onto 'md' | 'json' | 'txt'."""
        clean = (fmt or "").strip().lower().lstrip(".")
        clean = {"markdown": "md", "text": "txt", "plain": "txt"}.get(clean, clean)
        if clean not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported export format '{fmt}'. Use: md, json, txt")
        return clean

    @classmethod
    def default_filename(cls, fmt: str) -> str:
        """Timestamped default filename for the given format."""
        clean = cls.normalize_format(fmt)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"smartlog_export_{stamp}.{clean}"

    @staticmethod
    def _get_level_str(entry: LogEntry) -> str:
        """Extract a clean uppercase string representation of log level."""
        level = entry.level or "INFO"
        return str(getattr(level, "value", level)).upper()

    def build_metadata(
        self,
        entries: list[LogEntry],
        text_filter: str | None = None,
        min_level: Any = None,
    ) -> dict[str, Any]:
        """Build the shared metadata / header payload for an export."""
        level_summary: dict[str, int] = {}
        for entry in entries:
            level = self._get_level_str(entry)
            level_summary[level] = level_summary.get(level, 0) + 1

        date_from, date_to = self._date_range(entries)

        return {
            "application": f"{APP_NAME} v{APP_VERSION}",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_logs": len(entries),
            "date_range": {"from": date_from, "to": date_to},
            "active_filters": {
                "text_contains": text_filter,
                "minimum_level": getattr(min_level, "value", min_level),
            },
            "source_files": list(self.source_paths),
            "level_summary": dict(sorted(level_summary.items())),
        }

    def render(
        self,
        entries: list[LogEntry],
        fmt: str,
        text_filter: str | None = None,
        min_level: Any = None,
    ) -> str:
        """Render the full export (header/metadata + log data) as a string."""
        clean = self.normalize_format(fmt)
        metadata = self.build_metadata(entries, text_filter=text_filter, min_level=min_level)
        if clean == "md":
            return self._render_markdown(entries, metadata)
        if clean == "json":
            return self._render_json(entries, metadata)
        return self._render_text(entries, metadata)

    def export_to_file(
        self,
        entries: list[LogEntry],
        fmt: str,
        path: str | Path,
        text_filter: str | None = None,
        min_level: Any = None,
    ) -> tuple[bool, str]:
        """Render and write the export to a file. Returns (success, message)."""
        clean = self.normalize_format(fmt)
        content = self.render(entries, clean, text_filter=text_filter, min_level=min_level)
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return False, f"Could not write '{target}': {exc}"
        return True, str(target.resolve())

    def copy_to_clipboard(
        self,
        entries: list[LogEntry],
        fmt: str,
        text_filter: str | None = None,
        min_level: Any = None,
    ) -> tuple[bool, str]:
        """Render the export and copy it to the system clipboard."""
        clean = self.normalize_format(fmt)
        content = self.render(entries, clean, text_filter=text_filter, min_level=min_level)
        return SystemClipboard.copy(content)

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    @staticmethod
    def _describe_filters(filters: dict[str, Any]) -> str:
        parts: list[str] = []
        if filters.get("text_contains"):
            parts.append(f"contains '{filters['text_contains']}'")
        if filters.get("minimum_level"):
            parts.append(f"level >= {filters['minimum_level']}")
        return "; ".join(parts) if parts else "none (all logs)"

    @staticmethod
    def _md_escape(value: Any) -> str:
        if value is None:
            return "N/A"
        return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

    def _render_markdown(self, entries: list[LogEntry], meta: dict[str, Any]) -> str:
        lines: list[str] = [
            f"# {APP_NAME} — Log Export",
            "",
            "## Metadata",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Application | {meta['application']} |",
            f"| Exported at | {meta['exported_at']} |",
            f"| Total logs | {meta['total_logs']} |",
            f"| Date range | {meta['date_range']['from']} -> {meta['date_range']['to']} |",
            f"| Active filters | {self._md_escape(self._describe_filters(meta['active_filters']))} |",
            f"| Source files | {self._md_escape(', '.join(meta['source_files'])) or 'N/A'} |",
            "",
            "## Level Summary",
            "",
            "| Level | Count |",
            "| --- | --- |",
        ]
        for level, count in meta["level_summary"].items():
            lines.append(f"| {self._md_escape(level)} | {count} |")
        if not meta["level_summary"]:
            lines.append("| (no data) | 0 |")

        lines.extend([
            "",
            "## Log Data",
            "",
            "| # | Timestamp | Level | Source | Message |",
            "| --- | --- | --- | --- | --- |",
        ])
        for index, entry in enumerate(entries, 1):
            level_str = self._get_level_str(entry)
            lines.append(
                f"| {index}"
                f" | {self._md_escape(entry.timestamp)}"
                f" | {self._md_escape(level_str)}"
                f" | {self._md_escape(entry.source)}"
                f" | {self._md_escape(entry.raw)} |"
            )
        lines.append("")
        return "\n".join(lines)

    def _render_json(self, entries: list[LogEntry], meta: dict[str, Any]) -> str:
        payload = {
            "metadata": meta,
            "logs": [
                {
                    "index": index,
                    "timestamp": entry.timestamp,
                    "level": self._get_level_str(entry),
                    "source": entry.source,
                    "line_number": getattr(entry, "line_number", None),
                    "message": getattr(entry, "message", entry.raw),
                    "raw": entry.raw,
                    "metadata": getattr(entry, "metadata", {}),
                }
                for index, entry in enumerate(entries, 1)
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    def _render_text(self, entries: list[LogEntry], meta: dict[str, Any]) -> str:
        bar = "=" * 62
        thin = "-" * 62
        lines: list[str] = [
            bar,
            f" {APP_NAME} - Log Export".ljust(62),
            bar,
            f" Exported at    : {meta['exported_at']}",
            f" Total logs     : {meta['total_logs']}",
            f" Date range     : {meta['date_range']['from']}  ->  {meta['date_range']['to']}",
            f" Active filters : {self._describe_filters(meta['active_filters'])}",
            f" Source files   : {', '.join(meta['source_files']) or 'N/A'}",
            bar,
            thin,
        ]
        for index, entry in enumerate(entries, 1):
            timestamp = entry.timestamp or "-"
            source = entry.source or "-"
            level_str = self._get_level_str(entry)
            lines.append(
                f" {index:>6} | {timestamp:<20} | {level_str:<8} | {source:<24} | {entry.raw}"
            )
        lines.extend([thin, f" End of export - {meta['total_logs']} entries", bar, ""])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Timestamp helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        formats = (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S,%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%b %d %H:%M:%S",
            "%Y-%m-%d",
        )
        for fmt in formats:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _date_range(self, entries: list[LogEntry]) -> tuple[str, str]:
        min_dt: datetime | None = None
        max_dt: datetime | None = None
        min_str = max_str = "N/A"
        for entry in entries:
            parsed = self._parse_timestamp(entry.timestamp)
            if parsed is None:
                continue
            if min_dt is None or parsed < min_dt:
                min_dt, min_str = parsed, str(entry.timestamp)
            if max_dt is None or parsed > max_dt:
                max_dt, max_str = parsed, str(entry.timestamp)
        return min_str, max_str


# ---------------------------------------------------------------------------
# CLI helper: read a whole log file (used by `main.py --export`)
# ---------------------------------------------------------------------------

def read_log_file(path: str | Path, encoding: str = "utf-8") -> list[LogEntry]:
    """
    Read a whole log file and parse every line into a structured LogEntry.
    Used by the non-interactive CLI export (`python main.py --export ...`).
    """
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Log file not found: {path}")

    parser_host = LogStreamReader([str(source)], buffer_size=1)

    entries: list[LogEntry] = []
    with open(source, "r", encoding=encoding, errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if not line:
                continue
            try:
                entry = parser_host._parse_line(line, str(source), line_number)
                if entry is not None:
                    entries.append(entry)
            except Exception:
                # Fallback if parser raises on malformed line
                entries.append(
                    LogEntry(
                        timestamp=None,
                        level="INFO",
                        source=str(source),
                        raw=line,
                        message=line,
                        line_number=line_number,
                    )
                )
    return entries
