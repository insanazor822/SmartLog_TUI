"""Command line interface.

Replaces the original ``--export``-only CLI with a proper subcommand set and
global options that flow through to the TUI, reader and exporters:

    smartlog watch   app.log nginx.log      # live TUI (default command)
    smartlog watch   --demo                 # realistic generated stream
    smartlog scan    app.log                # one-shot batch analysis
    smartlog export  app.log -f html        # non-interactive export
    smartlog doctor                          # environment / provider check
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .exporters import Exporter, default_filename, read_log_file
from .filters import FilterEngine, build_spec
from .models import Level
from .parser import LineParser
from .stats import LogStats

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

LEVEL_CHOICES = [lv.label for lv in sorted(Level)]


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", type=Path,
                        help="log files or directories to read")
    parser.add_argument("-m", "--min-level", choices=LEVEL_CHOICES, default=None,
                        help="only report entries at or above this level")
    parser.add_argument("-g", "--grep", metavar="TEXT", default=None,
                        help="comma separated substrings; all must match")
    parser.add_argument("-v", "--exclude", metavar="TEXT", default=None,
                        help="comma separated substrings to drop")
    parser.add_argument("-s", "--source", action="append", default=None,
                        metavar="NAME", help="restrict to this source (repeatable)")
    parser.add_argument("--errors-only", action="store_true",
                        help="only entries that are errors or worse")
    parser.add_argument("-n", "--tail", type=int, default=0, metavar="N",
                        help="only look at the last N lines of each file")
    parser.add_argument("--encoding", default="utf-8", metavar="CS",
                        help="text encoding of the log files "
                             "(default: utf-8; try latin-1 or cp1252)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartlog",
        description="SmartLog — fast log analyser with cascading diagnostics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  smartlog watch --demo                     live demo stream
  smartlog watch app.log -m ERROR            tail, errors only
  smartlog watch /var/log/syslog -g timeout  tail, matching 'timeout'
  smartlog scan app.log --errors-only        one-shot analysis, no TUI
  smartlog export app.log -f html -o out.html write a report
  smartlog doctor                           check LLM providers and paths
""",
    )
    parser.add_argument("--version", action="version",
                        version=f"smartlog {__version__}")
    sub = parser.add_subparsers(dest="command")

    watch = sub.add_parser("watch", help="live tailing TUI (default)")
    _add_common(watch)
    watch.add_argument("--demo", action="store_true",
                       help="generate a realistic synthetic stream")
    watch.add_argument("--buffer", type=int, default=50_000, metavar="N",
                       help="ring-buffer size (default: 50000)")
    watch.add_argument("--from-start", action="store_true",
                       help="show existing content instead of tailing from EOF")
    watch.add_argument("--no-anomaly", action="store_true",
                       help="disable spike detection")
    watch.add_argument("--offline-diag", action="store_true",
                       help="never call an LLM; use the offline rule base")

    scan = sub.add_parser("scan", help="one-shot batch analysis")
    _add_common(scan)
    scan.add_argument("--top", type=int, default=10, metavar="N",
                      help="how many error signatures to show (default: 10)")

    export = sub.add_parser("export", help="export entries without the TUI")
    _add_common(export)
    export.add_argument("-f", "--format", default="md",
                        choices=["md", "markdown", "json", "txt", "text", "html"])
    export.add_argument("-o", "--output", default=None, metavar="PATH",
                        help="destination (default: ./smartlog_<stamp>.<ext>)")
    export.add_argument("--clipboard", action="store_true",
                        help="copy to the system clipboard instead of writing a file")
    export.add_argument("--all", action="store_true",
                        help="ignore filters and export every parsed entry")

    sub.add_parser("doctor", help="diagnose the environment")

    return parser


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

def resolve_paths(raw: Sequence[Path], *, limit: int = 200) -> list[Path]:
    """Expand directories into log files and keep only readable regular files."""
    out: list[Path] = []
    for item in raw:
        if item.is_dir():
            try:
                candidates = sorted(
                    p for p in item.rglob("*")
                    if (p.is_file() and p.suffix.lower() in
                    {".log", ".txt", ".out", ".err"}) or
                    (p.is_file() and any(k in p.name.lower()
                                         for k in ("log", "error", "access")))
                )
            except OSError:
                continue
            out.extend(candidates[:limit])
        elif item.is_file():
            out.append(item)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in out:
        resolved = path.resolve()
        if resolved not in seen and os.access(resolved, os.R_OK):
            seen.add(resolved)
            unique.append(resolved)
    return unique


def spec_from_args(args: argparse.Namespace) -> FilterEngine:
    min_level = Level[args.min_level] if getattr(args, "min_level", None) else None
    spec = build_spec(
        min_level=min_level,
        sources=getattr(args, "source", None) or [],
        include=getattr(args, "grep", None),
        exclude=getattr(args, "exclude", None),
        error_only=bool(getattr(args, "errors_only", False)),
    )
    return FilterEngine(spec)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_scan(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.paths)
    if not paths:
        print("no readable log files given", file=sys.stderr)
        return EXIT_USAGE

    parser = LineParser()
    engine = spec_from_args(args)
    stats = LogStats()
    entries = []

    for path in paths:
        try:
            file_entries = read_log_file(path, parser=parser,
                                        tail_lines=args.tail or None,
                                        encoding=getattr(args, "encoding", "utf-8"))
        except (OSError, FileNotFoundError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        matched = engine.apply(file_entries)
        entries.extend(matched)
        stats.record_many(matched)

    if not entries:
        print("no entries matched the active filters", file=sys.stderr)
        return EXIT_OK

    snapshot = stats.snapshot()
    errors = [e for e in entries if e.is_error]

    print(f"\nfiles      {len(paths)}")
    print(f"entries    {len(entries):,} "
          f"(errors {len(errors):,}, {len(errors) / len(entries) * 100:.1f}%)")
    print(f"filters    {engine.spec.describe()}")

    if snapshot.top_fingerprints:
        print(f"\ntop error signatures ({min(args.top, len(snapshot.top_fingerprints))}):")
        peak = snapshot.top_fingerprints[0][1] or 1
        for name, count in snapshot.top_fingerprints[: args.top]:
            bar = "█" * max(1, round(count / peak * 40))
            print(f"  {name:<24} {count:>7,}  {bar}")

    print("\nlast errors:")
    for entry in errors[-8:]:
        stamp = entry.timestamp or "-"
        print(f"  {stamp}  {entry.level.label:<8} {entry.raw[:110]}")
    print()
    # `scan` is a report, not a gate: errors are the expected input.
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    paths = resolve_paths(args.paths)
    if not paths:
        print("no readable log files given", file=sys.stderr)
        return EXIT_USAGE

    parser = LineParser()
    engine = FilterEngine() if args.all else spec_from_args(args)
    stats = LogStats()
    entries: list = []
    for path in paths:
        try:
            matched = engine.apply(read_log_file(
                path, parser=parser, tail_lines=args.tail or None,
                encoding=getattr(args, "encoding", "utf-8"),
            ))
        except (OSError, FileNotFoundError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        entries.extend(matched)
        stats.record_many(matched)

    if not entries:
        print("nothing to export", file=sys.stderr)
        return EXIT_OK

    exporter = Exporter()
    snapshot = stats.snapshot()
    sources = [str(p) for p in paths]
    filters = engine.spec.describe()

    if args.clipboard:
        ok, message = exporter.copy(
            entries, args.format,
            sources=sources, filters=filters, stats=snapshot,
        )
        if not ok:
            print(f"error: {message}", file=sys.stderr)
            return EXIT_ERROR
        print(f"{message} ({len(entries):,} entries, {args.format})")
        return EXIT_OK

    destination = args.output or default_filename(args.format)
    try:
        written = exporter.write(
            entries, args.format, destination,
            sources=sources, filters=filters, stats=snapshot,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    size = written.stat().st_size
    print(f"wrote {written} ({len(entries):,} entries, {size / 1024:.1f} KiB)")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report environment readiness. Never fails the process on a missing LLM."""
    from .diagnostics import discover_providers

    print(f"smartlog {__version__}")
    print(f"python     {platform.python_version()} ({sys.executable})")
    print(f"platform   {platform.system()} {platform.release()}")

    for name in ("textual", "rich"):
        try:
            module = __import__(name)
            print(f"{name:<10} {getattr(module, '__version__', 'installed')}")
        except ImportError:
            print(f"{name:<10} MISSING (pip install textual)")

    from .exporters import Clipboard

    backends = Clipboard.available_backends()
    print(f"clipboard  {', '.join(backends) if backends else 'none available'}")

    providers = discover_providers(force=True)
    print(f"\nLLM providers ({len(providers)} configured):")
    for provider in providers:
        auth = "authenticated" if provider.api_key else "no api key"
        print(f"  - {provider.label:<44} {auth}")

    print("\nEnvironment:")
    for key in ("SMARTLOG_LLM_URL", "SMARTLOG_LLM_MODEL", "SMARTLOG_LLM_API_KEY",
                "OPENAI_API_KEY", "OPENAI_MODEL", "SMARTLOG_NO_LOCAL"):
        value = os.getenv(key)
        if value:
            shown = value if "KEY" not in key else f"<set, {len(value)} chars>"
            print(f"  {key:<22} {shown}")
    print("\nOffline rule base always works; no provider is required.")
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    generator = None
    paths: list[str] = []

    if args.demo:
        from .demo import DemoGenerator

        generator = DemoGenerator()
        paths = [str(generator.start())]
    else:
        resolved = resolve_paths(args.paths)
        if not resolved:
            print("no readable log files given (try: smartlog watch --demo)",
                  file=sys.stderr)
            return EXIT_USAGE
        paths = [str(p) for p in resolved]

    spec = spec_from_args(args).spec
    try:
        from .app import SmartLogApp

        app = SmartLogApp(
            paths,
            buffer_size=args.buffer,
            start_at_end=not args.from_start,
            filter_spec=spec,
            offline_diagnostics=args.offline_diag,
            encoding=getattr(args, "encoding", "utf-8"),
        )
        app.run()
    except ImportError as exc:
        print(f"error: the TUI needs textual — pip install textual ({exc})",
              file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        pass
    finally:
        if generator is not None:
            generator.stop()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # `smartlog app.log` is shorthand for `smartlog watch app.log`
    if argv and not argv[0].startswith("-") and argv[0] not in {
        "watch", "scan", "export", "doctor"
    }:
        argv.insert(0, "watch")
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        return EXIT_OK if argv else EXIT_USAGE

    args = parser.parse_args(argv)

    handlers = {
        "watch": cmd_watch,
        "scan": cmd_scan,
        "export": cmd_export,
        "doctor": cmd_doctor,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return handler(args)
    except KeyboardInterrupt:
        print()
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())