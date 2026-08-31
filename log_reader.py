"""
Asynchronous Log Stream Reader for SmartLog TUI
Non-blocking file reading with real-time tail functionality.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiofiles

# Pre-compiled Regex patterns for high-throughput log parsing
LOG_PATTERNS = [
    # ISO 8601 timestamp + level + message
    re.compile(
        r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*\[?(\w+)\]?:\s*(.*)',
        re.IGNORECASE,
    ),
    # Syslog format
    re.compile(
        r'^([a-zA-Z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+(\S+?)(?:\[\d+\])?:\s*(.*)',
        re.IGNORECASE,
    ),
    # Apache/Nginx combined log format
    re.compile(
        r'^(\S+)\s+\S+\s+\S+\s+\[([^\]]+)\]\s+"([^"]+)"\s+(\d{3})\s+(\d+)',
        re.IGNORECASE,
    ),
    # Generic level detection anywhere in prefix
    re.compile(
        r'^(.*?)\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL)\b\s*[:\-\]]?\s*(.*)',
        re.IGNORECASE,
    ),
]

LEVEL_SEARCH_REGEX = re.compile(
    r'\b(CRITICAL|FATAL|ERROR|WARN(?:ING)?|DEBUG|INFO|NOTICE|TRACE)\b',
    re.IGNORECASE,
)


@dataclass
class LogEntry:
    """Represents a single parsed log entry."""

    timestamp: str
    level: str
    message: str
    raw: str
    source: str
    line_number: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class LogStreamReader:
    """
    Async log stream reader that monitors files for new content.

    Supports concurrent multi-file tailing, rotation/truncation detection,
    and O(1) ring-buffered memory management.
    """

    def __init__(
        self,
        log_paths: list[str],
        buffer_size: int = 1000,
        encoding: str = 'utf-8',
    ) -> None:
        self.log_paths: set[Path] = {Path(p) for p in log_paths}
        self.buffer_size = max(10, buffer_size)
        self.encoding = encoding

        # O(1) FIFO buffer
        self._buffer: deque[LogEntry] = deque(maxlen=self.buffer_size)
        self._file_positions: dict[Path, int] = {}
        self._file_inodes: dict[Path, int] = {}
        self._file_line_counts: dict[Path, int] = {}

        self._paused = False
        self._running = False

        self._queue: asyncio.Queue[tuple[Path, str, int]] = asyncio.Queue(maxsize=10000)
        self._worker_tasks: dict[Path, asyncio.Task[None]] = {}
        self._main_task: asyncio.Task[None] | None = None

        self._callbacks: list[Callable[[LogEntry], Any]] = []
        self._error_callbacks: list[Callable[[Exception], Any]] = []

    def add_callback(self, callback: Callable[[LogEntry], Any]) -> None:
        """Add a callback for new log entries."""
        self._callbacks.append(callback)

    def add_error_callback(self, callback: Callable[[Exception], Any]) -> None:
        """Add a callback for read errors."""
        self._error_callbacks.append(callback)

    def _notify_error(self, error: Exception) -> None:
        """Notify all registered error callbacks."""
        for callback in self._error_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback(error))
                else:
                    callback(error)
            except Exception:
                pass

    async def _get_file_stat(self, path: Path) -> tuple[int, int]:
        """Get inode and file size asynchronously."""
        try:
            loop = asyncio.get_running_loop()
            stat = await loop.run_in_executor(None, path.stat)
            return stat.st_ino, stat.st_size
        except OSError:
            return 0, 0

    async def _tail_file_worker(self, path: Path) -> None:
        """Continuously tail a specific log file and put new lines in the queue."""
        while self._running:
            try:
                if not path.exists():
                    await asyncio.sleep(0.5)
                    continue

                current_inode, current_size = await self._get_file_stat(path)
                stored_inode = self._file_inodes.get(path, current_inode)
                self._file_inodes[path] = current_inode

                # Check for rotation or truncation
                pos = self._file_positions.get(path, current_size)
                if current_inode != stored_inode or current_size < pos:
                    pos = 0
                    self._file_positions[path] = 0
                    self._file_line_counts[path] = 0

                async with aiofiles.open(path, mode='r', encoding=self.encoding, errors='replace') as f:
                    await f.seek(pos)

                    while self._running:
                        if self._paused:
                            await asyncio.sleep(0.1)
                            continue

                        line = await f.readline()
                        if line:
                            line_clean = line.rstrip('\r\n')
                            self._file_positions[path] = await f.tell()
                            self._file_line_counts[path] = self._file_line_counts.get(path, 0) + 1

                            if line_clean:
                                await self._queue.put((path, line_clean, self._file_line_counts[path]))
                        else:
                            # Verify if file was truncated/rotated while open
                            chk_inode, chk_size = await self._get_file_stat(path)
                            if chk_inode != self._file_inodes.get(path) or chk_size < self._file_positions.get(path, 0):
                                break  # Break out to re-open rotated file
                            await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._notify_error(e)
                await asyncio.sleep(1.0)

    def add_log_path(self, path: str) -> None:
        """Dynamically add and begin monitoring a new log path."""
        p = Path(path)
        if p not in self.log_paths:
            self.log_paths.add(p)
            if self._running:
                self._worker_tasks[p] = asyncio.create_task(self._tail_file_worker(p))

    def remove_log_path(self, path: str) -> None:
        """Stop monitoring and remove a log path."""
        p = Path(path)
        if p in self.log_paths:
            self.log_paths.remove(p)
            task = self._worker_tasks.pop(p, None)
            if task:
                task.cancel()

    async def start(self) -> None:
        """Start reading all configured log files."""
        self._running = True
        self._paused = False

        # Initialize file positions to EOF for tailing
        for path in self.log_paths:
            if path.exists():
                inode, size = await self._get_file_stat(path)
                self._file_inodes[path] = inode
                self._file_positions[path] = size
            else:
                self._file_positions[path] = 0

            # Spawn tail worker task per file
            self._worker_tasks[path] = asyncio.create_task(self._tail_file_worker(path))

        # Main consumer loop
        try:
            while self._running:
                try:
                    path, line, line_no = await asyncio.wait_for(self._queue.get(), timeout=0.2)
                except (asyncio.TimeoutError, TimeoutError):
                    continue

                entry = self._parse_line(line, str(path), line_no)
                if entry:
                    self._buffer.append(entry)

                    # Notify callbacks
                    for callback in self._callbacks:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                asyncio.create_task(callback(entry))
                            else:
                                callback(entry)
                        except Exception as e:
                            self._notify_error(e)

                self._queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    def _parse_line(self, line: str, source: str, line_number: int = 0) -> LogEntry:
        """Parse a log line into a structured entry using compiled patterns."""
        # 1. ISO 8601 Timestamp pattern
        match = LOG_PATTERNS[0].match(line)
        if match:
            ts, lvl, msg = match.groups()
            lvl_clean = 'WARN' if lvl.upper() == 'WARNING' else lvl.upper()
            return LogEntry(
                timestamp=ts,
                level=lvl_clean,
                message=msg,
                raw=line,
                source=source,
                line_number=line_number,
            )

        # 2. Syslog pattern with smart level detection
        match = LOG_PATTERNS[1].match(line)
        if match:
            ts, host, tag, msg = match.groups()
            detected_level = 'INFO'
            level_match = LEVEL_SEARCH_REGEX.search(msg)
            if level_match:
                lvl_val = level_match.group(1).upper()
                detected_level = 'WARN' if lvl_val == 'WARNING' else lvl_val

            return LogEntry(
                timestamp=ts,
                level=detected_level,
                message=msg,
                raw=line,
                source=source,
                line_number=line_number,
                metadata={'host': host, 'tag': tag},
            )

        # 3. Apache/Nginx Web server format
        match = LOG_PATTERNS[2].match(line)
        if match:
            ip, ts, req, status_str, size = match.groups()
            status_code = int(status_str) if status_str.isdigit() else 200
            if status_code >= 500:
                level = 'ERROR'
            elif status_code >= 400:
                level = 'WARN'
            else:
                level = 'INFO'
            return LogEntry(
                timestamp=ts,
                level=level,
                message=f"{req} -> {status_code} ({size} bytes)",
                raw=line,
                source=source,
                line_number=line_number,
                metadata={'ip': ip, 'status_code': status_code, 'bytes': size},
            )

        # 4. Generic level anywhere pattern
        match = LOG_PATTERNS[3].match(line)
        if match:
            prefix, lvl, msg = match.groups()
            lvl_clean = 'WARN' if lvl.upper() == 'WARNING' else lvl.upper()
            return LogEntry(
                timestamp=prefix.strip(),
                level=lvl_clean,
                message=msg.strip() or line,
                raw=line,
                source=source,
                line_number=line_number,
            )

        # Default unparsed entry
        return LogEntry(
            timestamp='',
            level='INFO',
            message=line,
            raw=line,
            source=source,
            line_number=line_number,
        )

    def pause(self) -> None:
        """Pause log reading."""
        self._paused = True

    def resume(self) -> None:
        """Resume log reading."""
        self._paused = False

    async def stop(self) -> None:
        """Stop log reading and cancel all workers cleanly."""
        self._running = False
        for task in self._worker_tasks.values():
            task.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks.values(), return_exceptions=True)
        self._worker_tasks.clear()

    def is_paused(self) -> bool:
        """Check if reading is paused."""
        return self._paused

    def get_buffer(self) -> list[LogEntry]:
        """Get snapshot of the current log buffer."""
        return list(self._buffer)

    def clear_buffer(self) -> None:
        """Clear the log buffer."""
        self._buffer.clear()

    def get_last_n_entries(self, n: int) -> list[LogEntry]:
        """Get the last n entries from the buffer."""
        if n <= 0:
            return []
        buf_len = len(self._buffer)
        slice_start = max(0, buf_len - n)
        return [self._buffer[i] for i in range(slice_start, buf_len)]


class LogStreamManager:
    """Manages multiple log streams and provides a unified interface."""

    def __init__(self, buffer_size: int = 1000) -> None:
        self._reader: LogStreamReader | None = None
        self._task: asyncio.Task[None] | None = None
        self._buffer_size = buffer_size

    async def initialize(
        self,
        log_paths: list[str],
        buffer_size: int | None = None,
        max_buffer_size: int | None = None,
    ) -> None:
        """Initialize the log reader with specified paths and optional custom buffer size."""
        effective_size = max_buffer_size or buffer_size or self._buffer_size
        self._buffer_size = effective_size
        self._reader = LogStreamReader(log_paths, buffer_size=self._buffer_size)

    def add_log_path(self, path: str) -> None:
        """Add a log path to monitor."""
        if self._reader:
            self._reader.add_log_path(path)

    def remove_log_path(self, path: str) -> None:
        """Remove a log path from monitoring."""
        if self._reader:
            self._reader.remove_log_path(path)

    async def start(self) -> None:
        """Start the log reader in background task."""
        if self._reader and not self._task:
            self._task = asyncio.create_task(self._reader.start())

    async def stop(self) -> None:
        """Stop the log reader cleanly."""
        if self._reader:
            await self._reader.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def pause(self) -> None:
        """Pause log reading."""
        if self._reader:
            self._reader.pause()

    def resume(self) -> None:
        """Resume log reading."""
        if self._reader:
            self._reader.resume()

    def is_paused(self) -> bool:
        """Check if reading is paused."""
        return self._reader.is_paused() if self._reader else True

    def add_callback(self, callback: Callable[[LogEntry], Any]) -> None:
        """Add a callback for new log entries."""
        if self._reader:
            self._reader.add_callback(callback)

    def add_error_callback(self, callback: Callable[[Exception], Any]) -> None:
        """Add an error callback."""
        if self._reader:
            self._reader.add_error_callback(callback)

    def get_buffer(self) -> list[LogEntry]:
        """Get the current log buffer."""
        return self._reader.get_buffer() if self._reader else []

    def clear_buffer(self) -> None:
        """Clear the log buffer."""
        if self._reader:
            self._reader.clear_buffer()

    def get_last_n_entries(self, n: int) -> list[LogEntry]:
        """Get the last n entries from the buffer."""
        return self._reader.get_last_n_entries(n) if self._reader else []
