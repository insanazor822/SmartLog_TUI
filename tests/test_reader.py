"""Reader tests: tailing, rotation, truncation, batching, backpressure."""

from __future__ import annotations

import asyncio
import gzip
import os
import time
from pathlib import Path

import pytest

from smartlog.models import Level
from smartlog.reader import LogStream


async def drain(stream: LogStream, *, want: int = 1, timeout: float = 5.0):
    """Collect entries until ``want`` have arrived or the deadline passes."""
    collected: list = []
    deadline = time.monotonic() + timeout
    while len(collected) < want and time.monotonic() < deadline:
        try:
            batch = await asyncio.wait_for(stream._queue.get(), timeout=0.25)
        except TimeoutError:
            continue
        stream._queue.task_done()
        collected.extend(batch)
    return collected


async def await_workers(stream: LogStream, count: int, *, timeout: float = 5.0) -> None:
    """Wait until ``stream`` has registered ``count`` worker threads.

    ``LogStream.add_path`` inserts the worker into the registry and starts its
    thread, but the thread has not run its first ``os.stat``/``seek`` when the
    call returns. A test that appends to the new path immediately afterwards
    races that first read, so the line can be missed.

    Only the registry is checked here — deliberately *not* the follow state. A
    worker for a path that does not exist yet reports ``missing``, which is
    exactly the state the test is about to resolve by writing the file, so
    waiting for it to clear would deadlock.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = stream.worker_health()
        if len(health) >= count and all(alive for _, alive, _ in health):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"expected {count} live worker(s), got {stream.worker_health()}"
    )


def rotate(path: Path, rotated: Path, *, attempts: int = 20) -> None:
    """Rename ``path`` to ``rotated``, tolerating Windows sharing delays.

    On Windows a rename fails with ``WinError 32`` while *any* process still has
    the file open without ``FILE_SHARE_DELETE``. The reader opens with
    delete-sharing (see ``smartlog.reader._open_shared``), but a handle can
    still be in flight, so a short retry keeps the test deterministic.
    """
    last: OSError | None = None
    for _ in range(attempts):
        try:
            os.replace(path, rotated)
            return
        except PermissionError as exc:      # WinError 32 / EACCES
            last = exc
            time.sleep(0.05)
    raise last if last is not None else OSError("rotation failed")


@pytest.fixture
def log(tmp_path: Path) -> Path:
    path = tmp_path / "app.log"
    path.write_text("", encoding="utf-8")
    return path


class TestTailing:
    @pytest.mark.asyncio
    async def test_reads_new_lines(self, log: Path):
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("2026-08-30 20:00:01 ERROR something broke\n")
                handle.flush()
            entries = await drain(stream, want=1)
            assert entries
            assert entries[0].level is Level.ERROR
            assert entries[0].line_number == 1
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_start_at_end_ignores_existing(self, log: Path):
        log.write_text("2026-08-30 20:00:01 INFO old line\n", encoding="utf-8")
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("2026-08-30 20:00:02 INFO new line\n")
                handle.flush()
            entries = await drain(stream, want=1)
            assert len(entries) == 1
            assert "new line" in entries[0].raw
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_from_start_reads_history(self, log: Path):
        log.write_text(
            "2026-08-30 20:00:01 INFO first\n"
            "2026-08-30 20:00:02 INFO second\n",
            encoding="utf-8",
        )
        stream = LogStream([log], start_at_end=False)
        await stream.start()
        try:
            entries = await drain(stream, want=2)
            assert [e.line_number for e in entries] == [1, 2]
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_partial_line_is_buffered_until_complete(self, log: Path):
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("INFO this line is incom")
                handle.flush()
            await asyncio.sleep(0.35)
            assert stream.snapshot() == []      # must not emit a torn line
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("plete now\n")
                handle.flush()
            entries = await drain(stream, want=1)
            assert entries[0].raw == "INFO this line is incomplete now"
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_multi_file(self, tmp_path: Path):
        a, b = tmp_path / "a.log", tmp_path / "b.log"
        a.write_text("", encoding="utf-8")
        b.write_text("", encoding="utf-8")
        stream = LogStream([a, b], start_at_end=True)
        await stream.start()
        try:
            for path, tag in ((a, "alpha"), (b, "bravo")):
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(f"INFO from {tag}\n")
                    handle.flush()
            entries = await drain(stream, want=2)
            messages = {e.message for e in entries}
            assert messages == {"from alpha", "from bravo"}
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_rotation_is_followed(self, log: Path):
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("INFO before rotation\n")
                handle.flush()
            await drain(stream, want=1)

            rotate(log, log.with_suffix(".log.1"))
            log.write_text("INFO after rotation\n", encoding="utf-8")

            entries = await drain(stream, want=1, timeout=6.0)
            assert entries, "did not follow the rotated file"
            assert "after rotation" in entries[0].raw
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_truncation_is_followed(self, log: Path):
        log.write_text("INFO line one\nINFO line two\n", encoding="utf-8")
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            await asyncio.sleep(0.2)
            # Truncate in place: same inode, smaller file.
            with open(log, "w", encoding="utf-8") as handle:
                handle.write("INFO fresh after truncate\n")
            entries = await drain(stream, want=1, timeout=6.0)
            assert entries
            assert "fresh after truncate" in entries[0].raw
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_gzipped_rotation_leaves_rotated_file(self, tmp_path: Path):
        log = tmp_path / "app.log"
        log.write_text("INFO original\n", encoding="utf-8")
        rotated = tmp_path / "app.log.1"
        os.replace(log, rotated)
        with gzip.open(f"{rotated}.gz", "wb") as handle:
            handle.write(b"INFO original\n")
        os.remove(rotated)
        log.write_text("", encoding="utf-8")

        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("INFO live again\n")
                handle.flush()
            entries = await drain(stream, want=1)
            assert entries[0].message == "live again"
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_missing_file_is_tolerated(self, tmp_path: Path):
        missing = tmp_path / "not-there.log"
        stream = LogStream([missing], start_at_end=True)
        await stream.start()
        try:
            await asyncio.sleep(0.3)
            assert stream.snapshot() == []
            missing.write_text("INFO appeared later\n", encoding="utf-8")
            entries = await drain(stream, want=1, timeout=6.0)
            assert entries[0].message == "appeared later"
        finally:
            await stream.stop()


class TestBuffer:
    @pytest.mark.asyncio
    async def test_ring_buffer_is_bounded(self, log: Path):
        stream = LogStream([log], buffer_size=64, start_at_end=False)
        await stream.start()
        try:
            with open(log, "a", encoding="utf-8") as handle:
                for i in range(500):
                    handle.write(f"INFO line {i}\n")
            await drain(stream, want=500, timeout=10.0)
            assert len(stream.snapshot()) <= 64
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_tail_returns_latest_in_order(self, log: Path):
        log.write_text(
            "".join(f"INFO line {i}\n" for i in range(10)), encoding="utf-8"
        )
        stream = LogStream([log], buffer_size=100, start_at_end=False)
        await stream.start()
        try:
            await drain(stream, want=10)
            tail = stream.tail(3)
            assert [e.message for e in tail] == ["line 7", "line 8", "line 9"]
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_clear_empties_buffer(self, log: Path):
        log.write_text("INFO x\n", encoding="utf-8")
        stream = LogStream([log], start_at_end=False)
        await stream.start()
        try:
            await drain(stream, want=1)
            stream.clear()
            assert stream.snapshot() == []
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_single_store_only(self, log: Path):
        """Memory must not be duplicated: one buffer, no shadow copy."""
        log.write_text("INFO x\n" * 50, encoding="utf-8")
        stream = LogStream([log], buffer_size=100, start_at_end=False)
        await stream.start()
        try:
            await drain(stream, want=50)
            assert len(stream.snapshot()) == len(stream._buffer)
        finally:
            await stream.stop()


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_pause_and_resume(self, log: Path):
        """Pause must stop *reading* without losing anything written meanwhile."""
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            await asyncio.sleep(0.15)          # let the worker reach its idle loop
            stream.pause()
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("INFO while paused\n")
                handle.flush()
            await asyncio.sleep(0.4)
            assert stream.snapshot() == [], "paused worker must not publish"

            stream.resume()
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("INFO after resume\n")
                handle.flush()

            deadline = time.monotonic() + 6.0
            messages: set[str] = set()
            while len(messages) < 2 and time.monotonic() < deadline:
                messages |= {e.message for e in stream.snapshot()}
                await asyncio.sleep(0.05)
            # Nothing written during the pause is lost, just delivered later.
            assert messages == {"while paused", "after resume"}
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    @pytest.mark.flaky(reruns=2, reason="3.10 runner is ~2x slower; drain() deadline")
    async def test_add_and_remove_path(self, tmp_path: Path):
        first = tmp_path / "first.log"
        second = tmp_path / "second.log"
        first.write_text("", encoding="utf-8")
        stream = LogStream([first], start_at_end=True)
        await stream.start()
        try:
            stream.add_path(second, start_at_end=False)
            await await_workers(stream, 2)
            second.write_text("INFO dynamic\n", encoding="utf-8")
            entries = await drain(stream, want=1, timeout=6.0)
            assert entries[0].message == "dynamic"

            stream.remove_path(second)
            assert len(stream.paths()) == 1
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_stop_is_clean(self, log: Path):
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        await stream.stop()
        assert all(not alive for _, alive, _ in stream.worker_health())

    @pytest.mark.asyncio
    async def test_latin1_encoding_is_honoured(self, tmp_path: Path):
        log = tmp_path / "latin1.log"
        log.write_bytes(b"")
        stream = LogStream([log], start_at_end=True, encoding="latin-1")
        await stream.start()
        try:
            with open(log, "ab") as handle:
                handle.write("INFO café ouvert\n".encode("latin-1"))
                handle.flush()
            entries = await drain(stream, want=1, timeout=5.0)
            assert entries
            assert "café ouvert" in entries[0].message
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_undecodable_bytes_do_not_kill_the_worker(self, tmp_path: Path):
        log = tmp_path / "binary.log"
        log.write_bytes(b"")
        stream = LogStream([log], start_at_end=True)
        await stream.start()
        try:
            with open(log, "ab") as handle:
                handle.write(b"INFO \xff\xfe junk\n")
                handle.write(b"ERROR after the junk\n")
                handle.flush()
            entries = await drain(stream, want=2, timeout=5.0)
            assert len(entries) == 2
            assert entries[1].level is Level.ERROR
        finally:
            await stream.stop()

    @pytest.mark.asyncio
    async def test_error_callback_fires(self, tmp_path: Path):
        seen: list[str] = []
        stream = LogStream(
            [tmp_path / "x.log"],
            start_at_end=True,
            on_error=lambda exc, path: seen.append(type(exc).__name__),
        )
        await stream.start()
        try:
            await asyncio.sleep(0.2)
        finally:
            await stream.stop()
        # A missing file is not an error; nothing should have been raised.
        assert isinstance(seen, list)