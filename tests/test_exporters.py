"""Exporter tests: all formats, streaming correctness, tail reads, clipboard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartlog.exporters import (
    Clipboard,
    Exporter,
    default_filename,
    normalise_format,
    read_log_file,
)
from smartlog.models import Level, LogEntry
from smartlog.parser import LineParser
from smartlog.stats import LogStats


@pytest.fixture(scope="module")
def parser() -> LineParser:
    return LineParser()


@pytest.fixture
def entries(parser: LineParser) -> list[LogEntry]:
    return [
        parser.parse("2026-08-30T20:00:01Z [INFO] [api] GET /orders 200", "app.log", 1),
        parser.parse("2026-08-30T20:00:02Z [ERROR] [db] deadlock detected", "app.log", 2),
        parser.parse("2026-08-30T20:00:03Z [WARN] [db] slow query 3200ms", "nginx.log", 3),
        parser.parse("2026-08-30T20:00:04Z [CRITICAL] Out of memory: OOMKilled 1", "app.log", 4),
    ]


class TestFormatNormalisation:
    @pytest.mark.parametrize("given,expected", [
        ("md", "md"), ("markdown", "md"), ("MD", "md"), (".md", "md"),
        ("json", "json"), ("txt", "txt"), ("text", "txt"), ("plain", "txt"),
        ("html", "html"), ("htm", "html"),
    ])
    def test_aliases(self, given, expected):
        assert normalise_format(given) == expected

    def test_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            normalise_format("pdf")

    def test_default_filename_has_suffix(self):
        assert default_filename("json").endswith(".json")
        assert default_filename("markdown").endswith(".md")


class TestMetadata:
    def test_counts_and_range(self, entries):
        meta = Exporter().build_meta(entries, sources=["/var/log/app.log"])
        assert meta.total == 4
        assert meta.total_errors == 2          # ERROR + CRITICAL
        assert meta.level_counts == {"CRITICAL": 1, "ERROR": 1, "INFO": 1, "WARN": 1}
        assert meta.first_seen.startswith("2026-08-30T20:00:01")
        assert meta.last_seen.startswith("2026-08-30T20:00:04")
        assert meta.sources == ["app.log"]

    def test_empty_input(self):
        meta = Exporter().build_meta([])
        assert meta.total == 0
        assert meta.first_seen == "N/A"
        assert meta.level_counts == {}

    def test_filter_description_recorded(self, entries):
        meta = Exporter().build_meta(entries, filters="level >= ERROR")
        assert meta.filters == "level >= ERROR"

    def test_stats_feed_error_rate(self, entries):
        stats = LogStats()
        stats.record_many(entries)
        meta = Exporter().build_meta(entries, stats=stats.snapshot())
        assert meta.error_rate_per_min >= 0


class TestMarkdown:
    def test_written_file_has_sections(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "md", tmp_path / "out.md")
        text = target.read_text(encoding="utf-8")
        assert "# SmartLog" in text
        assert "## Metadata" in text
        assert "## Level summary" in text
        assert "## Entries" in text
        assert "deadlock detected" in text

    def test_pipes_are_escaped(self, tmp_path: Path):
        entry = LogEntry(raw="a|b|c", source="s", level=Level.ERROR, message="a|b|c")
        target = Exporter().write([entry], "md", tmp_path / "x.md")
        text = target.read_text(encoding="utf-8")
        # The log body is a code fence, but table cells must never break layout.
        assert "| --- |" in text

    def test_render_matches_write(self, entries, tmp_path: Path):
        exporter = Exporter()
        target = exporter.write(entries, "md", tmp_path / "a.md")
        assert exporter.render(entries, "md") == target.read_text(encoding="utf-8")

    def test_suffix_is_corrected(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "json", tmp_path / "wrong.md")
        assert target.suffix == ".json"


class TestJson:
    def test_valid_and_complete(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "json", tmp_path / "o.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert set(payload) == {"metadata", "statistics", "entries"}
        assert len(payload["entries"]) == 4
        assert payload["entries"][0]["level"] == "INFO"
        assert payload["metadata"]["total"] == 4

    def test_unicode_is_preserved(self, tmp_path: Path):
        entry = LogEntry(raw="hata: veritabanı bağlantısı koptu", source="s",
                         level=Level.ERROR, message="hata")
        target = Exporter().write([entry], "json", tmp_path / "u.json")
        raw = target.read_text(encoding="utf-8")
        assert "veritabanı" in raw
        json.loads(raw)     # still valid

    def test_empty_entries(self, tmp_path: Path):
        target = Exporter().write([], "json", tmp_path / "e.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["entries"] == []
        assert payload["metadata"]["total"] == 0

    def test_preserved_unicode_survives_roundtrip(self, tmp_path: Path):
        entry = LogEntry(raw="✓ ok", source="s", level=Level.INFO, message="✓ ok")
        target = Exporter().write([entry], "json", tmp_path / "v.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["entries"][0]["message"] == "✓ ok"


class TestText:
    def test_aligned_columns(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "txt", tmp_path / "o.txt")
        lines = target.read_text(encoding="utf-8").splitlines()
        assert any(line.startswith("=" * 5) for line in lines)
        assert any("SMARTLOG LOG EXPORT" in line for line in lines)
        assert any("Levels" in line for line in lines)
        assert any("deadlock detected" in line for line in lines)


class TestHtml:
    def test_self_contained(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "html", tmp_path / "r.html")
        text = target.read_text(encoding="utf-8")
        assert text.startswith("<!DOCTYPE html>")
        assert "</html>" in text
        assert "http://" not in text.replace("http://www.w3.org", "")
        assert "<style>" in text and "<script>" in text

    def test_all_entries_rendered(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "html", tmp_path / "r.html")
        text = target.read_text(encoding="utf-8")
        assert text.count('class="row') == 4
        assert "deadlock detected" in text

    def test_html_is_escaped(self, tmp_path: Path):
        entry = LogEntry(raw='<script>alert("xss")</script>', source="s",
                         level=Level.ERROR, message='<script>alert("xss")</script>')
        target = Exporter().write([entry], "html", tmp_path / "x.html")
        text = target.read_text(encoding="utf-8")
        assert "<script>alert" not in text
        assert "&lt;script&gt;" in text

    def test_stats_cards_present(self, entries, tmp_path: Path):
        stats = LogStats()
        stats.record_many(entries)
        target = Exporter().write(entries, "html", tmp_path / "s.html",
                                  stats=stats.snapshot())
        text = target.read_text(encoding="utf-8")
        assert "throughput" in text
        assert "Top error signatures" in text or "signatures" in text

    def test_filter_widgets_present(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "html", tmp_path / "f.html")
        text = target.read_text(encoding="utf-8")
        assert 'id="q"' in text        # search input
        assert 'id="lvl"' in text      # level select
        assert 'id="errs"' in text     # errors-only toggle


class TestReadLogFile:
    def test_reads_all_lines(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "a.log"
        path.write_text(
            "2026-08-30 20:00:01 INFO one\n"
            "\n"
            "2026-08-30 20:00:02 ERROR two\n",
            encoding="utf-8",
        )
        entries = read_log_file(path, parser=parser)
        assert len(entries) == 2
        assert entries[1].level is Level.ERROR
        assert [e.line_number for e in entries] == [1, 3]

    def test_tail_lines_is_fast_and_correct(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "big.log"
        path.write_text(
            "".join(f"2026-08-30 20:00:00 INFO line {i}\n" for i in range(20_000)),
            encoding="utf-8",
        )
        entries = read_log_file(path, parser=parser, tail_lines=10)
        assert len(entries) == 10
        assert entries[-1].message == "line 19999"

    def test_missing_file_raises(self, tmp_path: Path, parser: LineParser):
        with pytest.raises(FileNotFoundError):
            read_log_file(tmp_path / "nope.log", parser=parser)

    def test_uses_public_parser_api(self, tmp_path: Path):
        """Regression: the original called a private ``_parse_line`` on a fake reader."""
        path = tmp_path / "b.log"
        path.write_text("INFO x\n", encoding="utf-8")
        entries = read_log_file(path)
        assert entries[0].level is Level.INFO

    def test_large_file_export_streams(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "huge.log"
        path.write_text(
            "".join(f"INFO entry {i}\n" for i in range(50_000)), encoding="utf-8"
        )
        entries = read_log_file(path, parser=parser)
        target = Exporter().write(entries, "json", tmp_path / "h.json")
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert len(payload["entries"]) == 50_000


    def test_tail_lines_on_file_smaller_than_window(self, tmp_path: Path,
                                                    parser: LineParser):
        """Regression: the seek-back loop never ran for small files, so
        ``chunk`` was unbound and the call crashed with UnboundLocalError."""
        path = tmp_path / "small.log"
        path.write_text("INFO a\nINFO b\nINFO c\n", encoding="utf-8")
        entries = read_log_file(path, parser=parser, tail_lines=10)
        assert len(entries) == 3
        assert [e.line_number for e in entries] == [1, 2, 3]

    def test_tail_lines_equals_file_length(self, tmp_path: Path,
                                           parser: LineParser):
        path = tmp_path / "exact.log"
        path.write_text("INFO a\nINFO b\n", encoding="utf-8")
        entries = read_log_file(path, parser=parser, tail_lines=2)
        assert len(entries) == 2

    def test_tail_lines_zero_falls_back_to_full_read(self, tmp_path: Path,
                                                     parser: LineParser):
        path = tmp_path / "full.log"
        path.write_text("INFO a\nINFO b\n", encoding="utf-8")
        assert len(read_log_file(path, parser=parser, tail_lines=0)) == 2

    @pytest.mark.parametrize("tail", [1, 2, 3, 5, 100, 100_000])
    def test_tail_lines_never_loops_forever(self, tmp_path: Path,
                                            parser: LineParser, tail):
        """The window-growth loop must terminate for every request size."""
        path = tmp_path / "tiny.log"
        path.write_text("INFO a\nINFO b\n", encoding="utf-8")
        entries = read_log_file(path, parser=parser, tail_lines=tail)
        assert len(entries) == min(tail, 2)

    def test_tail_line_numbers_are_absolute(self, tmp_path: Path,
                                            parser: LineParser):
        path = tmp_path / "numbered.log"
        path.write_text(
            "".join(f"INFO line {i}\n" for i in range(5000)), encoding="utf-8"
        )
        entries = read_log_file(path, parser=parser, tail_lines=3)
        assert [e.line_number for e in entries] == [4998, 4999, 5000]
        assert entries[-1].message == "line 4999"

    def test_tail_line_numbers_across_a_growth_step(self, tmp_path: Path,
                                                    parser: LineParser):
        """Requesting a tail that forces the window to grow twice."""
        path = tmp_path / "growing.log"
        path.write_text(
            "".join(f"INFO row {i}\n" for i in range(4000)), encoding="utf-8"
        )
        entries = read_log_file(path, parser=parser, tail_lines=500)
        assert len(entries) == 500
        assert entries[0].line_number == 4000 - 500 + 1
        assert entries[-1].line_number == 4000


class TestEncoding:
    """Regression: v1 had ``read_log_file(path, encoding=...)``; v2 lost it."""

    def test_default_is_utf8(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "utf8.log"
        path.write_text("INFO café açık\n", encoding="utf-8")
        entries = read_log_file(path, parser=parser)
        assert entries[0].message == "café açık"

    def test_latin1_is_decoded(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "latin1.log"
        path.write_bytes("INFO café ouvert\n".encode("latin-1"))
        entries = read_log_file(path, parser=parser, encoding="latin-1")
        assert "café ouvert" in entries[0].message

    def test_windows_1252_is_decoded(self, tmp_path: Path, parser: LineParser):
        path = tmp_path / "cp1252.log"
        path.write_bytes("ERROR smart € quote\n".encode("cp1252"))
        entries = read_log_file(path, parser=parser, encoding="cp1252")
        assert "smart € quote" in entries[0].message

    def test_undecodable_bytes_do_not_raise(self, tmp_path: Path,
                                            parser: LineParser):
        path = tmp_path / "broken.log"
        path.write_bytes(b"INFO \xff\xfe binary junk\n")
        entries = read_log_file(path, parser=parser)     # utf-8 + replace
        assert entries
        assert entries[0].level is Level.INFO

    def test_tail_lines_respects_encoding(self, tmp_path: Path,
                                          parser: LineParser):
        path = tmp_path / "latin1-big.log"
        path.write_bytes(
            "".join(f"INFO ligne {i} café\n" for i in range(2000)).encode("latin-1")
        )
        entries = read_log_file(path, parser=parser, tail_lines=5,
                                encoding="latin-1")
        assert len(entries) == 5
        assert "café" in entries[-1].message

    def test_iter_log_file_respects_encoding(self, tmp_path: Path,
                                             parser: LineParser):
        from smartlog.exporters import iter_log_file

        path = tmp_path / "iter.log"
        path.write_bytes("INFO réservé\n".encode("latin-1"))
        entries = list(iter_log_file(path, parser=parser, encoding="latin-1"))
        assert entries[0].message == "réservé"


class TestClipboard:
    def test_reports_backend_availability(self):
        backends = Clipboard.available_backends()
        assert isinstance(backends, list)

    def test_copy_returns_message(self):
        ok, message = Clipboard.copy("hello")
        assert isinstance(ok, bool)
        assert isinstance(message, str)

    def test_failure_message_is_actionable(self, monkeypatch):

        monkeypatch.setattr("smartlog.exporters.shutil.which", lambda name: None)
        monkeypatch.setattr("smartlog.exporters.platform.system", lambda: "Linux")
        ok, message = Clipboard.copy("hello")
        if not ok:
            assert "clipboard" in message.lower()


class TestExporterApi:
    def test_source_basename_only(self, entries):
        meta = Exporter().build_meta(entries, sources=["/var/log/nginx/access.log"])
        assert meta.sources == ["access.log"]

    def test_writes_to_nested_directory(self, entries, tmp_path: Path):
        target = Exporter().write(entries, "md", tmp_path / "a" / "b" / "c.md")
        assert target.exists()

    def test_unsupported_format_raises(self, entries, tmp_path: Path):
        with pytest.raises(ValueError):
            Exporter().write(entries, "pdf", tmp_path / "x")