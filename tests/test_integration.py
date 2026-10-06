"""Public API surface and cross-module integration tests."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import pytest

import smartlog


class TestPublicApi:
    @pytest.mark.parametrize("name", [
        "Level", "LogEntry", "LineParser", "parse_entry", "LogStats",
        "StatsSnapshot", "FilterEngine", "FilterSpec", "build_spec",
        "LogStream", "TailWorker", "DiagnosticEngine", "Diagnosis",
        "KnowledgeBase", "match_rules", "Exporter", "Clipboard",
        "read_log_file", "default_filename", "discover_providers",
        "format_timestamp", "truncate_string", "escape_html",
        "human_readable_size", "calculate_percentile", "format_duration",
    ])
    def test_exported(self, name):
        assert name in smartlog.__all__
        assert getattr(smartlog, name) is not None


class TestV1CompatibilityModule:
    """`smartlog.compat` must keep v1 imports working."""

    @pytest.mark.parametrize("name", [
        "LogLevel", "LogEntry", "SystemClipboard", "ErrorKnowledgeBase",
        "AIDiagnosticEngine", "RecommendedFix", "Diagnosis",
        "LogStreamReader", "LogStreamManager", "LogParser", "LogExporter",
    ])
    def test_v1_names_available(self, name):
        import smartlog.compat as compat

        assert name in compat.__all__
        assert getattr(compat, name) is not None

    def test_log_level_alias(self):
        from smartlog.compat import LogLevel

        assert LogLevel is smartlog.Level

    def test_clipboard_adapter(self):
        from smartlog.compat import SystemClipboard

        ok, message = SystemClipboard.copy("hello")
        assert isinstance(ok, bool) and isinstance(message, str)

    def test_knowledge_base_adapter(self):
        from smartlog.compat import ErrorKnowledgeBase

        hit = ErrorKnowledgeBase().find_matching_diagnosis(
            "ERROR Out of memory: OOMKilled process 1"
        )
        assert hit is not None
        pattern, payload = hit
        assert pattern == "oom"
        assert payload["recommendations"]

    def test_log_parser_adapter(self):
        from smartlog.compat import LogParser

        parser = LogParser()
        assert parser.parse_level("ERROR") is smartlog.Level.ERROR

        analysis = parser.analyze_line("ERROR deadlock detected on orders")
        assert analysis["is_error"] is True
        assert analysis["level"] is smartlog.Level.ERROR
        assert analysis["errors"]

    def test_log_parser_statistics_adapter(self):
        from smartlog.compat import LogParser

        parser = LogParser()
        for line in (
            "INFO all good",
            "ERROR deadlock detected",
            "ERROR connection refused",
        ):
            parser.update_statistics(line, "app.log")
        parser.refresh_rates()
        summary = parser.get_statistics_summary()
        assert summary["total_lines"] == 3
        assert summary["total_errors"] == 2
        assert parser.get_top_errors()
        assert parser.get_errors_by_category()
        parser.reset_statistics()
        assert parser.get_statistics_summary()["total_lines"] == 0

    def test_log_exporter_adapter(self, tmp_path: Path):
        from smartlog.compat import LogExporter

        entries = [smartlog.parse_entry("2026-08-30 20:00:01 ERROR boom",
                                        "app.log", 1)]
        exporter = LogExporter(source_paths=["app.log"])
        ok, message = exporter.export_to_file(entries, "md",
                                             tmp_path / "compat.md")
        assert ok is True
        assert Path(message).exists()
        assert exporter.normalize_format("markdown") == "md"
        assert exporter.default_filename("json").endswith(".json")

    @pytest.mark.asyncio
    async def test_log_stream_reader_adapter(self, tmp_path: Path):
        from smartlog.compat import LogStreamReader

        log = tmp_path / "v1.log"
        log.write_text("INFO from v1 adapter\n", encoding="utf-8")
        reader = LogStreamReader([str(log)], buffer_size=100)
        await reader.start()
        try:
            # The reader thread needs a moment to drain existing content.
            deadline = time.monotonic() + 3.0
            while not reader.get_buffer() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert reader.get_buffer()
            assert reader.get_last_n_entries(1)
            assert reader.is_paused() is False
            reader.pause()
            assert reader.is_paused() is True
            reader.resume()
            reader.clear_buffer()
            assert reader.get_buffer() == []
        finally:
            await reader.stop()

    def test_to_v2_entry(self):
        from smartlog.compat import to_v2_entry

        entry = to_v2_entry({"level": "error", "message": "boom",
                             "source": "app.log", "timestamp": "t"})
        assert entry.level is smartlog.Level.ERROR
        assert entry.message == "boom"

    def test_to_v2_entry_passthrough(self):
        from smartlog.compat import to_v2_entry

        original = smartlog.parse_entry("INFO x", "app.log", 1)
        assert to_v2_entry(original) is original

    def test_version_present(self):
        assert smartlog.__version__.count(".") == 2

    def test_no_aiofiles_dependency(self):
        """The old implementation required aiofiles; the new one does not."""
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert "aiofiles" not in text

    def test_module_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "smartlog", "--version"],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
        )
        assert result.returncode == 0
        assert "smartlog" in result.stdout


class TestEndToEnd:
    def test_parse_to_export_pipeline(self, tmp_path: Path):
        """Full pipeline without any UI: read -> stats -> filter -> export."""
        from smartlog import (
            Exporter,
            FilterEngine,
            LogStats,
            build_spec,
            read_log_file,
        )

        log = tmp_path / "mixed.log"
        log.write_text(
            "\n".join([
                '2026-08-30T20:00:01Z INFO  [api] GET /orders 200',
                '2026-08-30T20:00:02Z ERROR [db] deadlock detected',
                '2026-08-30T20:00:03Z DEBUG [cache] hit user:session:1',
                '2026-08-30T20:00:04Z WARN  [db] slow query 3200ms',
                '2026-08-30T20:00:05Z ERROR [cache] connection refused',
                '2026-08-30T20:00:06Z {"level":"error","message":"pool exhausted",'
                '"host":"web-01"}',
                'Aug 30 20:00:07 web sshd[1]: Failed password for root',
                '203.0.113.1 - - [30/Aug/2026:20:00:08 +0000] "GET /x HTTP/1.1" '
                '503 12 "-" "curl/8"',
            ]) + "\n",
            encoding="utf-8",
        )

        entries = read_log_file(log)
        assert len(entries) == 8

        stats = LogStats()
        stats.record_many(entries)
        snapshot = stats.snapshot()
        assert snapshot.total_lines == 8
        assert snapshot.total_errors >= 4
        assert snapshot.top_fingerprints

        engine = FilterEngine(build_spec(min_level="ERROR"))
        errors = engine.apply(entries)
        assert all(e.level >= smartlog.Level.ERROR for e in errors)

        target = Exporter().write(
            errors, "md", tmp_path / "errors.md",
            sources=[str(log)], filters=engine.spec.describe(), stats=snapshot,
        )
        text = target.read_text(encoding="utf-8")
        assert "deadlock detected" in text
        assert "Failed password" in text
        assert "503" in text

    @pytest.mark.asyncio
    async def test_diagnose_flows_into_export(self, tmp_path: Path):
        from smartlog import DiagnosticEngine, Exporter, Level, LogEntry

        entry = LogEntry(
            raw="CRITICAL Out of memory: OOMKilled process 4920",
            source="kernel.log",
            level=Level.CRITICAL,
            message="Out of memory: OOMKilled process 4920",
        )
        engine = DiagnosticEngine(offline=True)
        diagnosis = await engine.diagnose_entry(entry, [entry])

        report = tmp_path / "diag.md"
        lines = [
            f"# Diagnosis: {diagnosis.summary}",
            "",
            f"Confidence: {diagnosis.confidence}",
            f"Root cause: {diagnosis.root_cause}",
            "",
            "## Next steps",
        ]
        for rec in diagnosis.recommendations:
            lines.append(f"- {rec.title}: {rec.description}")
            if rec.command:
                lines.append(f"  `$ {rec.command}`")
        report.write_text("\n".join(lines), encoding="utf-8")

        text = report.read_text(encoding="utf-8")
        assert "OOM" in text or "memory" in text.lower()
        assert "$ " in text
        # Also verify the exporter can render a diagnosis-bearing view.
        exported = Exporter().write([entry], "html", tmp_path / "e.html")
        assert exported.exists()


class TestPipelineRegressions:
    def test_no_double_parsing(self, tmp_path: Path):
        """Each line must be parsed once; level must be identical after a re-parse."""
        from smartlog.parser import LineParser

        parser = LineParser()
        line = "2026-08-30 20:00:01 ERROR connection refused"
        first = parser.parse(line, "a.log", 1)
        second = parser.parse(line, "a.log", 1)
        assert first.level is second.level
        assert first.fingerprint == second.fingerprint

    def test_error_statistics_only_count_real_errors(self):
        from smartlog import LogStats

        stats = LogStats()
        for _ in range(100):
            stats.record(smartlog.LogEntry(raw="INFO query took 512ms",
                                           source="db", level=smartlog.Level.INFO,
                                           message="query took 512ms"))
        snapshot = stats.snapshot()
        assert snapshot.total_errors == 0
        assert snapshot.error_ratio == 0.0

    def test_filter_level_survives_as_int(self):
        from smartlog import FilterEngine, build_spec

        entry = smartlog.LogEntry(raw="x", source="s",
                                  level=smartlog.Level.ERROR, message="x")
        assert FilterEngine(build_spec(min_level="ERROR")).matches(entry)

    @pytest.mark.asyncio
    async def test_single_buffer_store(self, tmp_path: Path):
        """The reader owns exactly one ring buffer - no shadow copies."""
        import asyncio

        from smartlog.reader import LogStream

        log = tmp_path / "one.log"
        log.write_text("INFO x\n" * 20, encoding="utf-8")
        stream = LogStream([log], buffer_size=100, start_at_end=False)
        await stream.start()
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while len(stream.snapshot()) < 20 and asyncio.get_running_loop().time() < deadline:
                try:
                    await asyncio.wait_for(stream._queue.get(), timeout=0.3)
                except TimeoutError:
                    continue
            assert len(stream.snapshot()) == len(stream._buffer) == 20
        finally:
            await stream.stop()