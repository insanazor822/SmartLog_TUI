#!/usr/bin/env python3
"""
SmartLog TUI - Smart Log Analyzer & AI Diagnostics Tool

A production-grade, asynchronous Terminal User Interface application
for real-time log monitoring, error detection, and AI-powered diagnostics.

Usage:
    python main.py [log_file_1] [log_file_2] ...
    python main.py --demo
    python main.py --license SL-PRO-XXXX-XXXX-XXXX
    python main.py --export md|json|txt|clipboard [log_files ...]
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path

# Python version check
if sys.version_info < (3, 10):
    print("ERROR: Python 3.10 or higher is required to run SmartLog TUI.", file=sys.stderr)
    sys.exit(1)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="smartlog",
        description="SmartLog TUI - Smart Log Analyzer & AI Diagnostics Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s                          # Monitor default system logs
    %(prog)s /var/log/syslog          # Monitor specific log file
    %(prog)s log1.log log2.log        # Monitor multiple files
    %(prog)s --demo                   # Run interactive live demo mode
    %(prog)s --license SL-PRO-KEY     # Activate license and exit
    %(prog)s --export md /var/log/syslog            # Export logs to Markdown file
    %(prog)s --export clipboard log1.log log2.log   # Copy logs to clipboard
        """,
    )

    parser.add_argument(
        'log_files',
        nargs='*',
        type=str,
        help='Log files to monitor. If not specified, uses default system logs.',
    )

    parser.add_argument(
        '--demo',
        action='store_true',
        help='Run in interactive demo mode with realistic live simulated logs',
    )

    parser.add_argument(
        '--license',
        type=str,
        metavar='KEY',
        help='Activate a SmartLog license key',
    )

    parser.add_argument(
        '--export',
        metavar='FORMAT',
        choices=['md', 'markdown', 'json', 'txt', 'text', 'clipboard'],
        help='Non-interactive Export & Share: write the given log files to a '
             'Markdown/JSON/Plain-Text file (md, json, txt) or copy them to the '
             'system clipboard (clipboard), then exit without launching the TUI. '
             'File export writes smartlog_export_<timestamp>.<ext> to the current directory.',
    )

    parser.add_argument(
        '--version',
        action='version',
        version='SmartLog TUI v1.0.0',
    )

    return parser.parse_args()


def get_default_log_paths() -> list[str]:
    """Get default existing log file paths based on host OS."""
    system = platform.system()
    candidates: list[str] = []

    if system == "Linux":
        candidates = [
            "/var/log/syslog",
            "/var/log/messages",
            "/var/log/nginx/error.log",
            "/var/log/apache2/error.log",
            "/var/log/auth.log",
        ]
    elif system == "Darwin":  # macOS
        candidates = [
            "/var/log/system.log",
            "/var/log/install.log",
            "/var/log/wifi.log",
        ]
    else:  # Windows (Look for common application/server text logs)
        temp_dir = tempfile.gettempdir()
        candidates = [
            os.path.join(temp_dir, "smartlog_app.log"),
            r"C:\inetpub\logs\LogFiles\W3SVC1\u_ex.log",
        ]

    # Filter to accessible existing files safely
    existing: list[str] = []
    for path_str in candidates:
        try:
            p = Path(path_str)
            if p.is_file() and os.access(p, os.R_OK):
                existing.append(path_str)
        except OSError:
            continue

    if not existing:
        # Fallback to a default temporary log path
        fallback = os.path.join(tempfile.gettempdir(), "smartlog_system.log")
        fallback_path = Path(fallback)
        if not fallback_path.exists():
            try:
                with open(fallback_path, 'w', encoding='utf-8') as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO [system] Initialized smartlog fallback log.\n")
            except OSError as e:
                print(f"WARNING: Could not create fallback log at {fallback}: {e}", file=sys.stderr)
        return [fallback]

    return existing


def create_demo_environment() -> tuple[Path, threading.Event]:
    """
    Create a demo log file and start an asynchronous background simulator
    that feeds live log events to test streaming, filters, and AI diagnostics.
    """
    demo_path = Path(tempfile.gettempdir()) / "smartlog_live_demo.log"
    stop_event = threading.Event()

    # Reset/Create file
    try:
        with open(demo_path, 'w', encoding='utf-8') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO [system] Demo stream initialized.\n")
    except OSError as e:
        print(f"ERROR: Failed to initialize demo log at {demo_path}: {e}", file=sys.stderr)

    def _demo_feeder() -> None:
        events = [
            "INFO Application booted successfully on port 8080",
            "INFO DB pool connected: postgres://app_user@10.0.0.4:5432/main",
            "DEBUG Cache hit for key: user:session:99283",
            "WARN High memory usage detected: 82% threshold exceeded",
            "INFO Handling incoming request GET /api/v1/orders HTTP/1.1",
            "ERROR Connection refused: Unable to connect to redis://127.0.0.1:6379",
            "INFO Retrying connection to Redis cluster (Attempt 1/3)...",
            "WARN Slow query detected: 3200ms in get_user_dashboard()",
            "ERROR 500 Internal Server Error: Database transaction deadlock detected",
            "CRITICAL Out of memory: OOMKilled process 4920 (node)",
            "INFO Circuit breaker OPEN for external payment gateway",
            "INFO System recovery initiated by watchdog daemon",
        ]
        idx = 0
        while not stop_event.is_set():
            # Wait for 1.5s or break immediately if stop_event is set
            if stop_event.wait(timeout=1.5):
                break

            log_line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {events[idx % len(events)]}\n"
            try:
                with open(demo_path, 'a', encoding='utf-8') as f:
                    f.write(log_line)
                    f.flush()
            except OSError:
                pass
            idx += 1

    feeder_thread = threading.Thread(target=_demo_feeder, name="SmartLogDemoFeeder", daemon=True)
    feeder_thread.start()
    return demo_path, stop_event


def run_cli_export(fmt: str, log_files: list[str]) -> int:
    """
    Non-interactive Export & Share: read whole log files, export them via
    the exporter module, print a status message, and return an exit code.
    """
    try:
        from exporter import LogExporter, read_log_file
    except ImportError as e:
        print(f"ERROR: Exporter module could not be imported: {e}", file=sys.stderr)
        return 1

    entries = []
    for path in log_files:
        try:
            entries.extend(read_log_file(path))
        except OSError as exc:
            print(f"WARNING: Skipping unreadable log file {path}: {exc}", file=sys.stderr)

    if not entries:
        print("ERROR: No log entries available to export.", file=sys.stderr)
        return 1

    exporter = LogExporter(source_paths=log_files)

    if fmt == "clipboard":
        # Clipboard sharing uses the Markdown rendering
        ok, message = exporter.copy_to_clipboard(entries, "md")
    else:
        target = Path(exporter.default_filename(fmt))
        ok, message = exporter.export_to_file(entries, fmt, target)

    if ok:
        print(f"[OK] {message} ({len(entries)} log entries)")
        return 0

    print(f"[ERR] {message}", file=sys.stderr)
    return 1


def main() -> None:
    """Main CLI execution flow."""
    args = parse_arguments()

    # Handle standalone license activation
    if args.license:
        try:
            from license import get_license_manager
            lm = get_license_manager()
            result = lm.activate_license(args.license)
            if result.get('success'):
                print(f"\n[✓] {result.get('message', 'License activated successfully!')}")
                sys.exit(0)
            else:
                print(f"\n[✗] Activation Error: {result.get('error', 'Invalid license')}", file=sys.stderr)
                sys.exit(1)
        except ImportError as e:
            print(f"ERROR: License manager module missing: {e}", file=sys.stderr)
            sys.exit(1)

    # Handle non-interactive Export & Share (exits before the TUI starts)
    if args.export:
        files = args.log_files or get_default_log_paths()
        sys.exit(run_cli_export(args.export, files))

    stop_demo_event: threading.Event | None = None

    # Determine monitoring log files
    if args.demo:
        demo_file, stop_demo_event = create_demo_environment()
        log_paths = [str(demo_file)]
    elif args.log_files:
        log_paths = args.log_files
    else:
        log_paths = get_default_log_paths()

    # Import UI and launch Textual application
    try:
        from ui import SmartLogApp
    except ImportError as e:
        print(f"ERROR: UI module could not be imported: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        app = SmartLogApp(log_paths=log_paths)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if stop_demo_event is not None:
            stop_demo_event.set()


if __name__ == "__main__":
    main()
