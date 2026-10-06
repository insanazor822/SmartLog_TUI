"""Asynchronous multi-file log tailer.

Architecture
------------
The original used ``aiofiles`` plus one task per file plus a bounded queue,
and handed **every single line** to the UI callback individually.

This implementation:

* runs **one long-lived thread per file** that reads in *batches* (a batch is
  drained from the OS pipe and parsed, then published once). This removes the
  per-line ``await``/``aiofiles`` overhead entirely and keeps the asyncio event
  loop free.
* publishes batches into an ``asyncio.Queue`` via ``call_soon_threadsafe``,
  coalescing them under backpressure instead of blocking a reader thread.
* maintains **exactly one** ring buffer. The UI renders from filtered views of
  that buffer, so memory is no longer duplicated into ``RichLog``'s own store.
* handles rotation, truncation, gzip-free re-open and disappearance cleanly, and
  keeps ``(inode, offset)`` state so a resume does not re-read the world.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .models import LogEntry
from .parser import LineParser

__all__ = ["FileState", "LogStream", "LogStreamManager", "TailWorker"]

# Tunables (seconds / counts).
_POLL_INTERVAL = 0.08        # idle poll cadence per file
_BATCH_BYTES = 1 << 18       # 256 KiB read window per syscall
_MAX_LINE_CHARS = 64 * 1024  # guard against a pathological unterminated line
_BATCH_FLUSH_LINES = 500     # publish at least this often when busy


@dataclass(slots=True)
class FileState:
    """Follow state for one file, kept across rotations."""

    path: Path
    inode: int = 0
    offset: int = 0
    line_number: int = 0
    partial: str = ""          # buffered bytes not yet terminated by a newline
    missing_since: float | None = None

    @property
    def display_name(self) -> str:
        return self.path.name or str(self.path)


class TailWorker:
    """Blocking reader for a single file, driven by a dedicated thread."""

    __slots__ = (
        "_encoding",
        "_interval",
        "_on_error",
        "_on_rotate",
        "_parser",
        "_paused",
        "_publish",
        "_started",
        "_stop",
        "_thread",
        "state",
    )

    def __init__(
        self,
        path: str | Path,
        publish: Callable[[list[LogEntry]], None],
        *,
        parser: LineParser | None = None,
        interval: float = _POLL_INTERVAL,
        start_at_end: bool = True,
        encoding: str = "utf-8",
        on_error: Callable[[Exception, Path], None] | None = None,
        on_rotate: Callable[[Path], None] | None = None,
    ) -> None:
        self.state = FileState(path=Path(path))
        self._parser = parser or LineParser()
        self._encoding = encoding
        self._publish = publish
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = interval
        self._started = start_at_end
        self._on_error = on_error
        self._on_rotate = on_rotate

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"tail:{self.state.path.name}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- reader loop ------------------------------------------------------ #

    def _run(self) -> None:
        state = self.state
        handle = None
        try:
            if self._started:
                self._prime_offset(state)
            while not self._stop.is_set():
                if self._paused.is_set():
                    time.sleep(self._interval)
                    continue
                if handle is None:
                    handle = self._open(state)
                    if handle is None:
                        if self._stop.wait(self._interval * 5):
                            break
                        continue

                batch, exhausted = self._drain(handle, state)
                if batch:
                    self._publish(batch)
                if exhausted:
                    # The handle is parked at EOF: this is the moment to notice
                    # that the path was rotated, truncated or recreated.
                    if self._is_stale(state):
                        with contextlib.suppress(OSError):
                            handle.close()
                        handle = None
                        continue
                    self._stop.wait(self._interval)
        except Exception as exc:  # pragma: no cover - defensive
            if self._on_error is not None:
                self._on_error(exc, state.path)
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    handle.close()

    def _prime_offset(self, state: FileState) -> None:
        """Position the follower at EOF so we only show *new* lines by default."""
        try:
            st = os.stat(state.path)
        except OSError:
            return
        state.inode = st.st_ino
        state.offset = st.st_size

    def _open(self, state: FileState) -> IO[str] | None:
        """Open the file, handling rotation/truncation. Returns None if absent."""
        try:
            st = os.stat(state.path)
        except OSError:
            state.missing_since = state.missing_since or time.monotonic()
            return None

        rotated = (
            state.inode != 0
            and (st.st_ino != state.inode or st.st_size < state.offset)
        )
        if rotated:
            if self._on_rotate is not None:
                self._on_rotate(state.path)
            state.offset = 0
            state.partial = ""
            state.line_number = 0

        try:
            handle = open(state.path, encoding=self._encoding,
                          errors="replace")
        except OSError as exc:
            if self._on_error is not None:
                self._on_error(exc, state.path)
            return None

        state.inode = st.st_ino
        state.missing_since = None
        if not rotated:
            try:
                handle.seek(state.offset)
            except OSError:
                handle.seek(0)
                state.offset = 0
        return handle

    def _is_stale(self, state: FileState) -> bool:
        """True when the path no longer matches the handle we are holding."""
        try:
            st = os.stat(state.path)
        except OSError:
            return True                     # disappeared: reopen (maybe recreated)
        if state.inode and st.st_ino != state.inode:
            return True                     # rotated via rename/create
        return st.st_size < state.offset     # truncated in place

    def _drain(self, handle: IO[str], state: FileState) -> tuple[list[LogEntry], bool]:
        """Read whatever is currently available. Returns ``(batch, exhausted)``."""
        entries: list[LogEntry] = []
        exhausted = True
        source = str(state.path)
        line_no = state.line_number

        while len(entries) < _BATCH_FLUSH_LINES:
            chunk = handle.read(_BATCH_BYTES)
            if not chunk:
                break
            exhausted = False

            buf = state.partial + chunk
            if "\n" not in buf:
                if len(buf) > _MAX_LINE_CHARS:
                    # Unterminated monster line: force it out to bound memory.
                    state.partial = ""
                    line_no += 1
                    entries.append(self._build(buf[:_MAX_LINE_CHARS], source, line_no))
                else:
                    state.partial = buf
                continue

            state.partial = ""
            for line in buf.split("\n"):
                line = line.rstrip("\r")
                if not line.strip():
                    continue
                line_no += 1
                entries.append(self._build(line, source, line_no))

        if entries:
            state.line_number = line_no
            try:
                state.offset = handle.tell()
            except OSError:
                state.offset += sum(len(e.raw) + 1 for e in entries)
        return entries, exhausted

    def _build(self, line: str, source: str, line_no: int) -> LogEntry:
        return self._parser.parse(line, source, line_no)


class LogStream:
    """Aggregates several :class:`TailWorker` instances into one entry point."""

    def __init__(
        self,
        paths: Iterable[str | Path],
        *,
        buffer_size: int = 50_000,
        parser: LineParser | None = None,
        interval: float = _POLL_INTERVAL,
        queue_size: int = 512,
        start_at_end: bool = True,
        encoding: str = "utf-8",
        on_error: Callable[[Exception, Path], None] | None = None,
        on_rotate: Callable[[Path], None] | None = None,
    ) -> None:
        self._parser = parser or LineParser()
        self.encoding = encoding
        self._buffer: deque[LogEntry] = deque(maxlen=max(64, buffer_size))
        self._workers: dict[Path, TailWorker] = {}
        self._lock = threading.Lock()
        self._queue: asyncio.Queue[list[LogEntry]] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._on_error = on_error
        self._on_rotate = on_rotate

        for path in paths:
            self._spawn(path, interval, start_at_end)

    # -- worker management ------------------------------------------------ #

    def _spawn(self, path: str | Path, interval: float, start_at_end: bool) -> None:
        p = Path(path)
        if p in self._workers:
            return

        def publish(batch: list[LogEntry]) -> None:
            with self._lock:
                self._buffer.extend(batch)
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self._offer(batch)
            else:
                loop.call_soon_threadsafe(self._offer, batch)

        self._workers[p] = TailWorker(
            p,
            publish,
            parser=self._parser,
            interval=interval,
            start_at_end=start_at_end,
            encoding=self.encoding,
            on_error=self._on_error,
            on_rotate=self._on_rotate,
        )

    def _offer(self, batch: list[LogEntry]) -> None:
        """Enqueue from the event loop; coalesce instead of blocking readers."""
        if self._queue.full():
            # Drain the oldest batch and merge: keeps the freshest data without
            # ever blocking a reader thread on a full queue.
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._dropped += len(batch)
            except asyncio.QueueEmpty:  # pragma: no cover - race
                pass
        try:
            self._queue.put_nowait(batch)
        except asyncio.QueueFull:  # pragma: no cover - race
            self._dropped += len(batch)

    def add_path(self, path: str | Path, *, start_at_end: bool = True) -> None:
        p = Path(path)
        if p in self._workers:
            return
        self._spawn(p, _POLL_INTERVAL, start_at_end)
        self._workers[p].start()

    def remove_path(self, path: str | Path) -> None:
        worker = self._workers.pop(Path(path), None)
        if worker is not None:
            worker.stop()

    def paths(self) -> list[str]:
        return [str(p) for p in self._workers]

    # -- lifecycle -------------------------------------------------------- #

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for worker in self._workers.values():
            worker.start()

    async def stop(self) -> None:
        for worker in list(self._workers.values()):
            worker.stop()
        self._loop = None

    def pause(self) -> None:
        for worker in self._workers.values():
            worker.pause()

    def resume(self) -> None:
        for worker in self._workers.values():
            worker.resume()

    @property
    def paused(self) -> bool:
        return all(w.paused for w in self._workers.values()) if self._workers else False

    # -- consumption ------------------------------------------------------ #

    async def batches(self, timeout: float = 0.15) -> AsyncIterator[list[LogEntry]]:
        """Async iterator yielding batches of new entries."""
        while True:
            try:
                batch = await asyncio.wait_for(self._queue.get(), timeout)
            except TimeoutError:
                continue
            self._queue.task_done()
            yield batch

    def snapshot(self) -> list[LogEntry]:
        """Copy of the ring buffer, newest last."""
        with self._lock:
            return list(self._buffer)

    def tail(self, n: int) -> list[LogEntry]:
        """Last ``n`` entries without materialising the whole buffer."""
        with self._lock:
            if n <= 0 or n >= len(self._buffer):
                return list(self._buffer)
            out: list[LogEntry] = []
            for entry in reversed(self._buffer):
                out.append(entry)
                if len(out) >= n:
                    break
            out.reverse()
            return out

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    @property
    def dropped(self) -> int:
        return self._dropped

    def worker_health(self) -> list[tuple[str, bool, str]]:
        """``(path, alive, state)`` for the status bar."""
        out: list[tuple[str, bool, str]] = []
        for path, worker in self._workers.items():
            if not worker.alive:
                state = "stopped"
            elif worker.paused:
                state = "paused"
            elif worker.state.missing_since is not None:
                state = "missing"
            else:
                state = f"@{worker.state.offset}"
            out.append((str(path), worker.alive, state))
        return out


class LogStreamManager:
    """Lifecycle wrapper used by the TUI and the CLI."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._stream: LogStream | None = None

    async def open(
        self, paths: Iterable[str | Path], **overrides: Any
    ) -> LogStream:
        params = {**self._kwargs, **overrides}
        self._stream = LogStream(paths, **params)
        await self._stream.start()
        return self._stream

    @property
    def stream(self) -> LogStream | None:
        return self._stream

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None