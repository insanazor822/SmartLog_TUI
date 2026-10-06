"""Parser tests: formats, levels, fingerprints, false-positive guards."""

from __future__ import annotations

import pytest

from smartlog.models import Level, parse_level_token
from smartlog.parser import LineParser


@pytest.fixture(scope="module")
def parser() -> LineParser:
    return LineParser()


class TestLevels:
    @pytest.mark.parametrize("token,expected", [
        ("INFO", Level.INFO),
        ("info", Level.INFO),
        ("  warn  ", Level.WARN),
        ("WARNING", Level.WARN),
        ("err", Level.ERROR),
        ("crit", Level.CRITICAL),
        ("panic", Level.FATAL),
        ("emergency", Level.FATAL),
        ("[fatal]", Level.FATAL),
        ("nonsense", Level.INFO),
    ])
    def test_parse_level_token(self, token, expected):
        assert parse_level_token(token) is expected

    def test_numeric_level_from_syslog_severity(self, parser):
        entry = parser.parse('{"severity": 3, "message": "db down"}')
        assert entry.level is Level.ERROR


class TestFormats:
    def test_iso_with_level(self, parser):
        entry = parser.parse(
            "2026-08-30T20:00:01.123Z [ERROR] [api] payment failed",
            source="app.log",
        )
        assert entry.level is Level.ERROR
        assert entry.timestamp == "2026-08-30T20:00:01.123Z"
        assert entry.message == "payment failed"
        assert entry.ts_epoch > 0

    def test_iso_space_separated(self, parser):
        entry = parser.parse("2026-08-30 20:00:01 WARN disk usage 91%")
        assert entry.level is Level.WARN
        assert entry.ts_epoch > 0

    def test_json_structured(self, parser):
        entry = parser.parse(
            '{"timestamp":"2026-08-30T20:00:01Z","level":"error",'
            '"message":"connection refused","host":"web-01"}',
            source="ignored",
        )
        assert entry.level is Level.ERROR
        assert entry.message == "connection refused"
        assert entry.source == "web-01"

    def test_json_without_level_key(self, parser):
        entry = parser.parse('{"msg":"plain info"}')
        assert entry.message == "plain info"
        assert entry.level is Level.INFO

    def test_syslog(self, parser):
        entry = parser.parse(
            "Aug 30 20:00:01 myhost sshd[1234]: Failed password for root",
            source="syslog",
        )
        assert entry.message.startswith("Failed password")
        assert entry.level is Level.ERROR
        assert entry.meta.get("host") == "myhost"
        assert entry.ts_epoch > 0

    def test_nginx_combined(self, parser):
        entry = parser.parse(
            '203.0.113.9 - frank [30/Aug/2026:20:00:01 +0000] '
            '"GET /api/v1/orders HTTP/1.1" 500 2326 "-" "curl/8.4.0"'
        )
        assert entry.level is Level.ERROR
        assert entry.meta["status_code"] == 500
        assert entry.meta["method"] == "GET"
        assert "500" in entry.message

    def test_nginx_404_is_warning(self, parser):
        entry = parser.parse(
            '10.0.0.1 - - [30/Aug/2026:20:00:01 +0000] '
            '"GET /nope HTTP/1.1" 404 120 "-" "curl/8"'
        )
        assert entry.level is Level.WARN

    def test_bracketed_level(self, parser):
        entry = parser.parse("[2026-08-30 20:00:01] [INFO] service started")
        assert entry.level is Level.INFO
        assert entry.message == "service started"

    def test_loose_level_prefix(self, parser):
        entry = parser.parse("nginx: [error] 123#0: open() failed")
        assert entry.level is Level.ERROR
        assert "open() failed" in entry.message

    def test_plain_line_does_not_crash(self, parser):
        entry = parser.parse("just some unstructured output")
        assert entry.level is Level.INFO
        assert entry.message == "just some unstructured output"

    def test_empty_line(self, parser):
        entry = parser.parse("   ")
        assert entry.raw == "   "

    def test_line_number_and_source_propagate(self, parser):
        entry = parser.parse("INFO x", source="a.log", line_number=42)
        assert entry.source == "a.log"
        assert entry.line_number == 42


class TestFalsePositives:
    """Regressions for the bug that flagged every bare 4xx/5xx number."""

    @pytest.mark.parametrize("line", [
        "INFO [db] query completed in 512ms",
        "INFO [db] query completed in 404 ms",
        "INFO [perf] 523 requests per second",
        "INFO [batch] processed 499 records in 1234ms",
    ])
    def test_latency_numbers_are_not_http_errors(self, parser, line):
        entry = parser.parse(line)
        assert not entry.is_error, f"{line!r} was misclassified"
        assert not entry.fingerprint.startswith(("http_",))

    def test_real_http_status_is_detected(self, parser):
        entry = parser.parse("WARN upstream returned status 502 from proxy")
        assert entry.fingerprint == "http_5xx"
        assert entry.level >= Level.WARN

    def test_four_oh_three_is_rate_limit(self, parser):
        entry = parser.parse("ERROR client 10.0.0.1 rate limited (429)")
        assert entry.fingerprint == "throttle"


class TestFingerprints:
    @pytest.mark.parametrize("line,expected", [
        ("CRITICAL Out of memory: OOMKilled process 4920", "oom"),
        ("ERROR No space left on device", "disk_full"),
        ("ERROR connection refused: redis://127.0.0.1:6379", "conn_refused"),
        ("ERROR deadlock detected on orders", "db_deadlock"),
        ("Traceback (most recent call last):", "python_traceback"),
        ("ERROR Permission denied: open '/etc/x'", "permission"),
        ("ERROR authentication failed for user admin", "auth_failure"),
        ("WARN slow query detected: 3200ms", "slow_query"),
        ("ERROR SSL handshake failed: certificate verify failed", "certs"),
        ("ERROR circuit breaker OPEN for gateway", "dependency_down"),
    ])
    def test_signature(self, parser, line, expected):
        assert parser.parse(line).fingerprint == expected

    def test_error_level_is_raised_by_pattern(self, parser):
        # Logged as INFO but is an out-of-memory event.
        entry = parser.parse("INFO process terminated: Out of memory")
        assert entry.is_error
        assert entry.level >= Level.CRITICAL

    def test_duration_extracted(self, parser):
        entry = parser.parse("WARN slow query detected: 3200ms in get_user()")
        assert entry.meta["duration_ms"] == 3200


class TestThroughput:
    def test_parsing_is_fast_enough(self, parser):
        """Sanity check: 20k lines must parse in well under a second."""
        import time

        lines = [
            "2026-08-30T20:00:01.123Z [INFO] [api] GET /api/v1/orders 200 12ms",
            "2026-08-30T20:00:02.456Z [DEBUG] [db] SELECT * FROM orders LIMIT 10",
            '{"timestamp":"2026-08-30T20:00:01Z","level":"info",'
            '"message":"request __N__"}',
        ]
        start = time.perf_counter()
        for i in range(20_000):
            parser.parse(lines[i % 3].replace("__N__", str(i)), "app.log", i)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"20k lines took {elapsed:.2f}s"


class TestEntryDict:
    def test_to_dict_is_json_safe(self, parser):
        import json

        entry = parser.parse('{"level":"error","message":"boom","host":"h1"}')
        payload = json.dumps(entry.to_dict())
        assert '"boom"' in payload
        assert entry.to_dict()["level"] == "ERROR"