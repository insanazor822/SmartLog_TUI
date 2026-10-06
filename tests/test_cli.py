"""CLI tests: argument parsing, path resolution, subcommand dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from smartlog.cli import build_parser, main, resolve_paths, spec_from_args


@pytest.fixture
def logfile(tmp_path: Path) -> Path:
    path = tmp_path / "app.log"
    path.write_text(
        "2026-08-30 20:00:01 INFO service started\n"
        "2026-08-30 20:00:02 ERROR deadlock detected on orders\n"
        "2026-08-30 20:00:03 WARN slow query detected: 3200ms\n"
        "2026-08-30 20:00:04 ERROR No space left on device\n",
        encoding="utf-8",
    )
    return path


class TestArgumentParsing:
    def test_bare_paths_become_watch(self):
        parser = build_parser()
        args = parser.parse_args(["watch", "a.log"])
        assert args.command == "watch"
        assert args.paths == [Path("a.log")]

    def test_shorthand_inserts_watch(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr("smartlog.cli.cmd_watch", lambda args: captured.update(
            vars(args)) or 0
        )
        assert main(["app.log"]) == 0
        assert captured["command"] == "watch"
        assert captured["paths"] == [Path("app.log")]

    def test_scan_options(self):
        args = build_parser().parse_args(
            ["scan", "a.log", "-m", "ERROR", "--errors-only", "-g", "db"]
        )
        assert args.command == "scan"
        assert args.min_level == "ERROR"
        assert args.errors_only is True
        assert args.grep == "db"

    def test_export_options(self):
        args = build_parser().parse_args(
            ["export", "a.log", "-f", "html", "-o", "r.html", "--clipboard"]
        )
        assert args.command == "export"
        assert args.format == "html"
        assert args.output == "r.html"
        assert args.clipboard is True

    def test_invalid_level_is_rejected(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "a.log", "-m", "NOPE"])

    def test_no_args_shows_usage(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().out.lower()

    def test_help_exits_zero(self, capsys):
        assert main(["--help"]) == 0
        assert "smartlog" in capsys.readouterr().out


class TestPathResolution:
    def test_single_file(self, logfile: Path):
        assert resolve_paths([logfile]) == [logfile.resolve()]

    def test_directory_expands_to_logs(self, tmp_path: Path):
        (tmp_path / "a.log").write_text("INFO x\n")
        (tmp_path / "error.txt").write_text("ERROR x\n")
        (tmp_path / "unrelated.bin").write_bytes(b"\x00")
        found = resolve_paths([tmp_path])
        names = {p.name for p in found}
        assert "a.log" in names
        assert "error.txt" in names
        assert "unrelated.bin" not in names

    def test_missing_paths_are_skipped(self, tmp_path: Path):
        assert resolve_paths([tmp_path / "nope.log"]) == []

    def test_duplicates_removed(self, logfile: Path, tmp_path: Path):
        found = resolve_paths([logfile, logfile, tmp_path / "."])
        assert len(found) == len(set(found))

    def test_recurses_nested_directories(self, tmp_path: Path):
        nested = tmp_path / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "deep.log").write_text("INFO x\n")
        found = resolve_paths([tmp_path])
        assert any(p.name == "deep.log" for p in found)


class TestSpecFromArgs:
    def test_builds_filter(self):
        args = build_parser().parse_args(
            ["scan", "a.log", "-m", "WARN", "-g", "timeout,redis",
             "-v", "healthz", "--errors-only", "-s", "app.log"]
        )
        engine = spec_from_args(args)
        spec = engine.spec
        assert spec.min_level.name == "WARN"
        assert set(spec.include) == {"timeout", "redis"}
        assert spec.exclude == ("healthz",)
        assert spec.error_only is True
        assert spec.sources == frozenset({"app.log"})

    def test_defaults_are_empty(self):
        engine = spec_from_args(build_parser().parse_args(["scan", "a.log"]))
        assert engine.spec.is_empty()


class TestScanCommand:
    def test_scan_reports_errors(self, logfile: Path, capsys):
        code = main(["scan", str(logfile)])
        out = capsys.readouterr().out
        assert code == 0
        assert "entries" in out
        assert "deadlock" in out

    def test_scan_filters_by_level(self, logfile: Path, capsys):
        main(["scan", str(logfile), "-m", "ERROR"])
        out = capsys.readouterr().out
        assert "last errors:" in out

    def test_scan_no_files(self, tmp_path: Path, capsys):
        assert main(["scan", str(tmp_path / "missing.log")]) == 2

    def test_scan_with_no_matches(self, logfile: Path, capsys):
        code = main(["scan", str(logfile), "-g", "nomatchxyz"])
        assert code == 0
        assert "no entries matched" in capsys.readouterr().err


class TestExportCommand:
    @pytest.mark.parametrize("fmt,suffix", [
        ("md", ".md"), ("json", ".json"), ("txt", ".txt"), ("html", ".html"),
    ])
    def test_each_format_writes(self, logfile: Path, tmp_path: Path, capsys,
                               fmt, suffix):
        out = tmp_path / f"report{suffix}"
        code = main(["export", str(logfile), "-f", fmt, "-o", str(out)])
        assert code == 0
        assert out.exists()
        assert out.suffix == suffix
        assert "wrote" in capsys.readouterr().out

    def test_default_output_name(self, logfile: Path, tmp_path: Path, capsys,
                                 monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert main(["export", str(logfile), "-f", "json"]) == 0
        written = list(tmp_path.glob("smartlog_*.json"))
        assert len(written) == 1

    def test_export_respects_filters(self, logfile: Path, tmp_path: Path, capsys):
        out = tmp_path / "only.md"
        main(["export", str(logfile), "-m", "ERROR", "-o", str(out)])
        text = out.read_text(encoding="utf-8")
        assert "deadlock" in text
        assert "service started" not in text

    def test_all_flag_ignores_filters(self, logfile: Path, tmp_path: Path):
        out = tmp_path / "all.md"
        main(["export", str(logfile), "-m", "FATAL", "-o", str(out), "--all"])
        assert "service started" in out.read_text(encoding="utf-8")

    def test_export_no_files(self, tmp_path: Path):
        assert main(["export", str(tmp_path / "missing.log")]) == 2

    def test_encoding_option_is_accepted(self):
        args = build_parser().parse_args(["scan", "a.log", "--encoding", "latin-1"])
        assert args.encoding == "latin-1"

    def test_encoding_defaults_to_utf8(self):
        assert build_parser().parse_args(["scan", "a.log"]).encoding == "utf-8"

    def test_scan_uses_requested_encoding(self, tmp_path: Path, capsys):
        log = tmp_path / "latin1.log"
        log.write_bytes("ERROR café ouvert\n".encode("latin-1"))
        assert main(["scan", str(log), "--encoding", "latin-1"]) == 0
        assert "café ouvert" in capsys.readouterr().out


class TestDoctorCommand:
    def test_doctor_runs(self, capsys):
        code = main(["doctor"])
        out = capsys.readouterr().out
        assert code == 0
        assert "python" in out
        assert "LLM providers" in out
        assert "offline" in out.lower() or "Offline" in out


class TestWatchCommand:
    def test_watch_requires_files_or_demo(self, tmp_path: Path, capsys):
        code = main(["watch", str(tmp_path / "missing.log")])
        assert code == 2
        assert "no readable log files" in capsys.readouterr().err

    def test_watch_passes_options_to_app(self, monkeypatch):
        """The TUI is never launched here; only its construction is verified."""
        import smartlog.app as app_module

        launched: dict = {}

        class FakeApp:
            def __init__(self, paths, **kwargs):
                launched["paths"] = list(paths)
                launched.update(kwargs)

            def run(self):
                launched["ran"] = True

        monkeypatch.setattr(app_module, "SmartLogApp", FakeApp)

        class FakeGenerator:
            def __init__(self, *a, **k):
                self.stopped = False

            def start(self):
                return Path("/tmp/smartlog-demo/app.log")

            def stop(self):
                self.stopped = True

        monkeypatch.setattr("smartlog.demo.DemoGenerator", FakeGenerator)

        code = main([
            "watch", "--demo", "--buffer", "1234", "--offline-diag",
            "--min-level", "ERROR", "-g", "redis",
        ])
        assert code == 0
        assert launched["ran"] is True
        assert launched["buffer_size"] == 1234
        assert launched["start_at_end"] is True
        assert launched["offline_diagnostics"] is True
        assert launched["filter_spec"].min_level.name == "ERROR"
        assert launched["filter_spec"].include == ("redis",)

    def test_watch_from_start_flag(self, logfile: Path, monkeypatch):
        import smartlog.app as app_module

        launched: dict = {}

        class FakeApp:
            def __init__(self, paths, **kwargs):
                launched.update(kwargs)

            def run(self):
                pass

        monkeypatch.setattr(app_module, "SmartLogApp", FakeApp)
        main(["watch", str(logfile), "--from-start"])
        assert launched["start_at_end"] is False