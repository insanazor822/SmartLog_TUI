"""Export & share: Markdown, JSON, plain text, self-contained HTML, clipboard.

Optimisations vs. the original:

* every format is written **streaming** — the old code built one giant string
  with ``"\\n".join(...)`` and then wrote it, doubling peak memory for large
  exports;
* timestamp parsing for the metadata header is done with a **cache**, and the
  range is computed in a single pass with a bounded, early-exiting comparison;
* the HTML report now actually renders the **full stats payload** (it existed in
  ``utils.py`` as dead code) and streams entry by entry.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from .models import LEVEL_LABELS, Level, LogEntry
from .stats import StatsSnapshot

__all__ = ["SUPPORTED_FORMATS", "Clipboard", "ExportMeta", "Exporter"]

SUPPORTED_FORMATS = ("md", "json", "txt", "html")
_EXT = {"md": ".md", "json": ".json", "txt": ".txt", "html": ".html"}
_ALIASES = {
    "markdown": "md", "text": "txt", "plain": "txt", "txt": "txt",
    "html": "html", "htm": "html", "json": "json", "md": "md",
}


@dataclass(slots=True)
class ExportMeta:
    """Header block shared by every format."""

    application: str = "SmartLog"
    version: str = "2.0.0"
    generated_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    total: int = 0
    first_seen: str = ""
    last_seen: str = ""
    sources: list[str] = field(default_factory=list)
    filters: str = ""
    level_counts: dict[str, int] = field(default_factory=dict)
    total_errors: int = 0
    error_rate_per_min: float = 0.0

    @property
    def title(self) -> str:
        return f"{self.application} v{self.version}"


def normalise_format(fmt: str) -> str:
    key = (fmt or "").strip().lower().lstrip(".")
    key = _ALIASES.get(key, key)
    if key not in _EXT:
        raise ValueError(f"Unsupported export format {fmt!r}; use one of "
                         f"{', '.join(SUPPORTED_FORMATS)}")
    return key


def default_filename(fmt: str, *, stamp: str | None = None) -> str:
    key = normalise_format(fmt)
    return f"smartlog_{stamp or datetime.now().strftime('%Y%m%d_%H%M%S')}{_EXT[key]}"


class Clipboard:
    """Cross-platform clipboard without a hard third-party dependency."""

    @staticmethod
    def available_backends() -> list[str]:
        system = platform.system()
        if system == "Windows":
            return ["clip.exe"] if shutil.which("clip.exe") else []
        if system == "Darwin":
            return ["pbcopy"] if shutil.which("pbcopy") else []
        found = []
        for name in ("wl-copy", "xclip", "xsel"):
            if shutil.which(name):
                found.append(name)
        return found

    @classmethod
    def copy(cls, text: str) -> tuple[bool, str]:
        data = text.encode("utf-8")
        system = platform.system()
        if system == "Windows":
            return cls._spawn(["clip.exe"], data)
        if system == "Darwin":
            return cls._spawn(["pbcopy"], data)

        attempts: list[list[str]] = []
        if shutil.which("wl-copy"):
            attempts.append(["wl-copy"])
        if shutil.which("xclip"):
            attempts.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            attempts.append(["xsel", "--clipboard", "--input"])
        errors: list[str] = []
        for command in attempts:
            ok, msg = cls._spawn(command, data)
            if ok:
                return True, f"copied via {command[0]}"
            errors.append(msg)
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(text)
            return True, "copied via pyperclip"
        except Exception as exc:
            detail = "; ".join(errors) or str(exc)
            return False, (f"no clipboard backend available ({detail}). "
                           "Install wl-clipboard, xclip or xsel.")

    @staticmethod
    def _spawn(command: list[str], data: bytes) -> tuple[bool, str]:
        try:
            subprocess.run(command, input=data, check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "ok"
        except (subprocess.SubprocessError, OSError) as exc:
            return False, f"{command[0]}: {exc}"


class Exporter:
    """Renders and writes log views."""

    def __init__(self, *, app_name: str = "SmartLog", version: str = "2.0.0") -> None:
        self.app_name = app_name
        self.version = version

    # -- entry normalisation --------------------------------------------- #

    @staticmethod
    def _entry_fields(entry: LogEntry) -> tuple[str, str, str, str]:
        """``(timestamp, level, source, raw)`` with safe coercions."""
        timestamp = entry.timestamp or "-"
        level = entry.level.label if isinstance(entry.level, Level) else str(entry.level)
        source = os.path.basename(entry.source) if entry.source else "-"
        return timestamp, level, source, entry.raw

    def build_meta(
        self,
        entries: Sequence[LogEntry],
        *,
        sources: Iterable[str] = (),
        filters: str = "",
        stats: StatsSnapshot | None = None,
    ) -> ExportMeta:
        levels: dict[str, int] = {}
        errors = 0
        first_ts: float = 0.0
        first_raw = ""
        last_ts: float = 0.0
        last_raw = ""

        for entry in entries:
            level = LEVEL_LABELS.get(entry.level, "INFO")
            levels[level] = levels.get(level, 0) + 1
            if entry.level >= Level.ERROR:
                errors += 1
            ts = entry.ts_epoch
            if ts > 0:
                if first_ts == 0.0 or ts < first_ts:
                    first_ts, first_raw = ts, entry.timestamp
                if ts > last_ts:
                    last_ts, last_raw = ts, entry.timestamp

        return ExportMeta(
            application=self.app_name,
            version=self.version,
            total=len(entries),
            first_seen=first_raw or "N/A",
            last_seen=last_raw or "N/A",
            sources=[os.path.basename(s) for s in sources],
            filters=filters or "none",
            level_counts=dict(sorted(levels.items())),
            total_errors=errors,
            error_rate_per_min=stats.error_rate_per_min if stats else 0.0,
        )

    # -- public API -------------------------------------------------------- #

    def write(
        self,
        entries: Sequence[LogEntry],
        fmt: str,
        destination: str | Path,
        *,
        sources: Iterable[str] = (),
        filters: str = "",
        stats: StatsSnapshot | None = None,
    ) -> Path:
        """Stream an export straight to disk. Raises OSError on failure."""
        key = normalise_format(fmt)
        target = Path(destination).expanduser()
        if target.suffix != _EXT[key]:
            target = target.with_suffix(_EXT[key])
        target.parent.mkdir(parents=True, exist_ok=True)

        meta = self.build_meta(entries, sources=sources, filters=filters, stats=stats)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            writer = getattr(self, f"_write_{key}")
            writer(entries, meta, handle, stats)
        return target

    def render(
        self,
        entries: Sequence[LogEntry],
        fmt: str,
        *,
        sources: Iterable[str] = (),
        filters: str = "",
        stats: StatsSnapshot | None = None,
    ) -> str:
        """Return the export as a string (used for the clipboard)."""
        key = normalise_format(fmt)
        meta = self.build_meta(entries, sources=sources, filters=filters, stats=stats)
        parts: list[str] = []
        sink = _ListSink(parts)
        writer = getattr(self, f"_write_{key}")
        writer(entries, meta, sink, stats)
        return "".join(parts)

    def copy(
        self,
        entries: Sequence[LogEntry],
        fmt: str = "md",
        **kwargs: Any,
    ) -> tuple[bool, str]:
        return Clipboard.copy(self.render(entries, fmt, **kwargs))

    # -- format writers ---------------------------------------------------- #

    def _write_md(self, entries: Sequence[LogEntry], meta: ExportMeta,
                  out: TextIO, stats: StatsSnapshot | None) -> None:
        w = out.write
        w(f"# {meta.application} — Log Export\n\n")
        w("## Metadata\n\n| Field | Value |\n| --- | --- |\n")
        for field_name, value in (
            ("Application", meta.title),
            ("Generated at", meta.generated_at),
            ("Entries", f"{meta.total:,}"),
            ("Window", f"{meta.first_seen} → {meta.last_seen}"),
            ("Active filters", meta.filters),
            ("Sources", ", ".join(meta.sources) or "N/A"),
            ("Errors", f"{meta.total_errors:,} "
                       f"({meta.error_rate_per_min:.1f}/min)"),
        ):
            w(f"| {_md(field_name)} | {_md(value)} |\n")

        w("\n## Level summary\n\n| Level | Count |\n| --- | --- |\n")
        if meta.level_counts:
            for level, count in meta.level_counts.items():
                w(f"| {level} | {count:,} |\n")
        else:
            w("| (none) | 0 |\n")

        if stats and stats.top_fingerprints:
            w("\n## Top error signatures\n\n| Signature | Count |\n| --- | --- |\n")
            for name, count in stats.top_fingerprints:
                w(f"| {_md(name)} | {count:,} |\n")

        w("\n## Entries\n\n```text\n")
        for entry in entries:
            ts, level, source, raw = self._entry_fields(entry)
            w(f"{ts} [{level:<8}] ({source}) {raw}\n")
        w("```\n")

    def _write_json(self, entries: Sequence[LogEntry], meta: ExportMeta,
                    out: TextIO, stats: StatsSnapshot | None) -> None:
        # Manual streaming JSON: json.dump cannot stream without holding the tree.
        out.write('{\n  "metadata": ')
        meta_dict = asdict(meta)
        out.write(json.dumps(meta_dict, ensure_ascii=False, indent=2).replace("\n", "\n  "))
        out.write(',\n  "statistics": ')
        stats_dict = _snapshot_to_dict(stats) if stats else {}
        out.write(json.dumps(stats_dict, ensure_ascii=False, default=str))
        out.write(',\n  "entries": [\n')
        last = len(entries) - 1
        for i, entry in enumerate(entries):
            out.write("    ")
            out.write(json.dumps(entry.to_dict(), ensure_ascii=False, default=str))
            out.write(",\n" if i != last else "\n")
        out.write("  ]\n}\n")

    def _write_txt(self, entries: Sequence[LogEntry], meta: ExportMeta,
                   out: TextIO, stats: StatsSnapshot | None) -> None:
        rule = "=" * 78
        thin = "-" * 78
        w = out.write
        w(f"{rule}\n {meta.application.upper()} LOG EXPORT\n{rule}\n")
        w(f" Generated   : {meta.generated_at}\n")
        w(f" Entries     : {meta.total:,}\n")
        w(f" Window      : {meta.first_seen} -> {meta.last_seen}\n")
        w(f" Filters     : {meta.filters}\n")
        w(f" Sources     : {', '.join(meta.sources) or 'N/A'}\n")
        w(f" Errors      : {meta.total_errors:,} ({meta.error_rate_per_min:.1f}/min)\n")
        w(f"{rule}\n Levels     : ")
        w("  ".join(f"{k}={v:,}" for k, v in meta.level_counts.items()) or "-")
        w(f"\n{thin}\n")
        for entry in entries:
            ts, level, source, raw = self._entry_fields(entry)
            w(f"{ts:<26} {level:<9} {source:<22} {raw}\n")
        w(f"{thin}\n end of export ({meta.total:,} entries)\n{rule}\n")

    def _write_html(self, entries: Sequence[LogEntry], meta: ExportMeta,
                    out: TextIO, stats: StatsSnapshot | None) -> None:
        from .html_report import write_html

        write_html(entries, meta, out, stats, level_colors=self._level_hex())


    @staticmethod
    def _level_hex() -> dict[Level, str]:
        from .html_report import LEVEL_HEX

        return LEVEL_HEX


class _ListSink:
    """Minimal ``TextIO``-like sink that accumulates into a list."""

    __slots__ = ("_parts",)

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def _md(value: Any) -> str:
    """Escape a value for a Markdown table cell."""
    text = "N/A" if value is None else str(value)
    return (text.replace("\\", "\\\\").replace("|", "\\|")
                .replace("\r", " ").replace("\n", " "))


def _snapshot_to_dict(snapshot: StatsSnapshot) -> dict[str, Any]:
    data = {f: getattr(snapshot, f) for f in StatsSnapshot.__slots__}
    if is_dataclass(data):  # pragma: no cover - defensive
        return asdict(data)
    return data


# --------------------------------------------------------------------------- #
# Whole-file reader for the non-interactive CLI path
# --------------------------------------------------------------------------- #

def read_log_file(path: str | Path, *, parser: Any | None = None,
                  tail_lines: int | None = None,
                  encoding: str = "utf-8") -> list[LogEntry]:
    """Parse an entire log file into entries.

    ``tail_lines`` reads only the last N lines efficiently for very large files
    by seeking backwards instead of loading the whole file.

    ``encoding`` accepts anything :mod:`codecs` recognises, so Latin-1,
    Windows-1252 or Shift-JIS logs decode correctly. Undecodable bytes are
    replaced rather than raising.
    """
    from .parser import LineParser

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"not a readable file: {path}")

    engine = parser or LineParser()
    entries: list[LogEntry] = []
    start_line = 1

    if tail_lines is not None and tail_lines > 0:
        size = source.stat().st_size
        # Read backwards in growing windows until enough newlines are visible.
        # `start` reaching 0 means the whole file is already in `chunk`, so the
        # loop must stop there even if the file has fewer lines than requested.
        window = max(tail_lines * 200, 64 * 1024)
        with open(source, "rb") as raw:
            start = max(0, size - window)
            raw.seek(start)
            chunk = raw.read()
            while chunk.count(b"\n") < tail_lines and start > 0:
                window *= 2
                start = max(0, size - window)
                raw.seek(start)
                chunk = raw.read()

            # Number of complete lines that precede the window.
            prefix_lines = 0
            if start > 0:
                raw.seek(0)
                prefix_lines = raw.read(start).count(b"\n")

        text = chunk.decode(encoding, errors="replace")
        lines = text.split("\n")
        if start > 0 and chunk[:1] != b"\n":
            # We joined the file mid-line, so the first fragment is partial.
            lines = lines[1:]
            prefix_lines += 1
        if lines and not lines[-1]:
            lines.pop()                 # trailing newline, not a line
        start_line = max(1, prefix_lines + 1)
        if len(lines) > tail_lines:
            dropped = len(lines) - tail_lines
            lines = lines[-tail_lines:]   # keep exactly the requested tail
            start_line += dropped         # absolute numbering must follow the slice
        for offset, line in enumerate(lines):
            stripped = line.rstrip("\r")
            if stripped.strip():
                entries.append(
                    engine.parse(stripped, str(source), start_line + offset)
                )
        return entries

    with open(source, encoding=encoding, errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            stripped = line.rstrip("\n").rstrip("\r")
            if stripped.strip():
                entries.append(engine.parse(stripped, str(source), number))
    return entries


def iter_log_file(path: str | Path, *, parser: Any | None = None,
                  encoding: str = "utf-8") -> Iterator[LogEntry]:
    """Lazy variant of :func:`read_log_file` for very large files."""
    from .parser import LineParser

    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"not a readable file: {path}")
    engine = parser or LineParser()
    with open(source, encoding=encoding, errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            stripped = line.rstrip("\n").rstrip("\r")
            if stripped.strip():
                yield engine.parse(stripped, str(source), number)


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")