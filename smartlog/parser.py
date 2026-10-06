"""Single-pass log line parser.

Design notes
------------
* Every line is parsed **exactly once**. The returned :class:`LogEntry` is the
  only representation that flows through the app, so no downstream module needs
  to re-parse levels, timestamps or messages.
* Multi-format support (JSON, ISO, syslog, combined web log, generic) is tried
  cheapest-first, guarded by cheap literal checks before expensive regexes.
* Expensive multi-pattern *error fingerprinting* only runs when a cheap
  substring prefilter or the level marks a real error candidate, which keeps
  steady-state INFO throughput an order of magnitude higher.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Final

from .models import Level, LogEntry, lookup_level, parse_level_token

__all__ = ["FINGERPRINTS", "Fingerprint", "LineParser", "parse_entry"]


# --------------------------------------------------------------------------- #
# Fingerprint rules: group similar errors, estimate severity
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A named error signature with a severity score."""

    name: str
    category: str
    severity: int
    pattern: re.Pattern[str]
    # Optional cheap literal prefilter; ``None`` means "always try the regex".
    hints: tuple[str, ...] | None = None

    def matches(self, text: str) -> str | None:
        if self.hints is not None and not any(h in text for h in self.hints):
            return None
        found = self.pattern.search(text)
        return found.group(0) if found else None


# NOTE: patterns must be specific enough not to fire on unrelated numbers.
# A previous version treated every bare ``5xx``/``4xx`` as an HTTP status and
# therefore flagged messages such as "query took 512ms" as server errors.
FINGERPRINTS: Final[tuple[Fingerprint, ...]] = (
    Fingerprint(
        name="http_5xx", category="HTTP", severity=45,
        pattern=re.compile(
            r"\b(?:status(?:_code)?|http(?:/\d(?:\.\d)?)?|response(?:_code)?|"
            r"code)\W{0,3}(5\d{2})\b|\b(?:HTTP\s*)?(?:5\d{2})\s+"
            r"(?:internal server error|bad gateway|service unavailable|"
            r"gateway time-?out)\b",
            re.IGNORECASE,
        ),
        hints=("5", "status", "http", "gateway"),
    ),
    Fingerprint(
        name="http_4xx", category="HTTP", severity=32,
        pattern=re.compile(
            r"\b(?:status(?:_code)?|http(?:/\d(?:\.\d)?)?|response(?:_code)?|"
            r"code)\W{0,3}(4\d{2})\b|\b(?:HTTP\s*)?(?:4\d{2})\s+"
            r"(?:bad request|unauthorized|forbidden|not found|too many requests)\b",
            re.IGNORECASE,
        ),
        hints=("4", "status", "http"),
    ),
    Fingerprint(
        name="python_traceback", category="Exception", severity=45,
        pattern=re.compile(
            r"\bTraceback \(most recent call last\)|\b[A-Za-z_][\w.]*"
            r"(?:Error|Exception)\b",
        ),
        hints=("traceback", "error", "exception"),
    ),
    Fingerprint(
        name="panic", category="Process", severity=60,
        pattern=re.compile(r"\b(?:panic|fatal error|segmentation fault|"
                           r"core dumped|abort(?:ed)? trap)\b", re.IGNORECASE),
        hints=("panic", "fatal", "segfault", "core dumped", "abort"),
    ),
    Fingerprint(
        name="oom", category="Resource", severity=55,
        pattern=re.compile(
            r"\bout of memory\b|\boom-?kill(?:ed)?\b|\bMemoryError\b|"
            r"\bJavaScript heap out of memory\b|\bcannot allocate memory\b",
            re.IGNORECASE,
        ),
        hints=("memory", "oom", "allocat"),
    ),
    Fingerprint(
        name="disk_full", category="Resource", severity=55,
        pattern=re.compile(
            r"\bno space left on device\b|\bdisk (?:is )?full\b|\bENOSPC\b",
            re.IGNORECASE,
        ),
        hints=("space", "disk", "enospc"),
    ),
    Fingerprint(
        name="db_deadlock", category="Database", severity=45,
        pattern=re.compile(
            r"\bdeadlock detected\b|\block wait timeout\b|"
            r"\b(?:connection|pool) exhausted\b",
            re.IGNORECASE,
        ),
        hints=("deadlock", "lock wait", "exhausted"),
    ),
    Fingerprint(
        name="db_error", category="Database", severity=42,
        pattern=re.compile(
            r"\b(?:sql|database|db)\s+(?:error|syntax|exception)\b|"
            r"\bquery failed\b|\b(?:unique|foreign key|not null) constraint\b|"
            r"\b(?:psycopg2|sqlalchemy|pymysql)\.[\w.]*(?:Error|Exception)\b",
            re.IGNORECASE,
        ),
        hints=("sql", "database", "db ", "query", "constraint"),
    ),
    Fingerprint(
        name="conn_refused", category="Network", severity=38,
        pattern=re.compile(
            r"\bconnection refused\b|\bECONNREFUSED\b|\bno route to host\b|"
            r"\bconnection reset by peer\b|\bECONNRESET\b",
            re.IGNORECASE,
        ),
        hints=("refused", "econn", "route to host"),
    ),
    Fingerprint(
        name="timeout", category="Network", severity=35,
        pattern=re.compile(
            r"\btimed? ?out\b|\bETIMEDOUT\b|\bdeadline exceeded\b|"
            r"\breadiness probe failed\b|\bcontext deadline\b",
            re.IGNORECASE,
        ),
        hints=("timed out", "timeout", "etimedout", "deadline"),
    ),
    Fingerprint(
        name="permission", category="Permission", severity=38,
        pattern=re.compile(
            r"\bpermission denied\b|\baccess denied\b|\bEACCES\b|\bEPERM\b|"
            r"\bunauthorized\b|\bforbidden\b",
            re.IGNORECASE,
        ),
        hints=("permission", "denied", "eacces", "eperm", "forbidden",
               "unauthorized"),
    ),
    Fingerprint(
        name="auth_failure", category="Security", severity=40,
        pattern=re.compile(
            r"\bauthentication fail(?:ed|ure)\b|\binvalid (?:credentials|token|"
            r"password|api key)\b|\blogin failed\b|\btoken (?:expired|revoked)\b|"
            r"\b(?:failed|invalid|incorrect) (?:password|credentials|login)\b",
            re.IGNORECASE,
        ),
        hints=("auth", "credential", "token", "login", "password"),
    ),
    Fingerprint(
        name="certs", category="Security", severity=42,
        pattern=re.compile(
            r"\b(?:x509|certificate verify failed|certificate has expired|"
            r"ssl handshake|unable to get local issuer|self signed)\b",
            re.IGNORECASE,
        ),
        hints=("certificate", "x509", "ssl", "tls", "issuer"),
    ),
    Fingerprint(
        name="dns", category="Network", severity=35,
        pattern=re.compile(
            r"\b(?:NXDOMAIN|name or service not known|"
            r"temporary failure in name resolution|dns resolution failed)\b",
            re.IGNORECASE,
        ),
        hints=("dns", "resolve", "nxdomain", "host not found"),
    ),
    Fingerprint(
        name="dependency_down", category="Availability", severity=40,
        pattern=re.compile(
            r"\b(?:circuit breaker (?:open|half[- ]open)|"
            r"service unavailable|no healthy upstream|"
            r"could not connect to (?:host|server|broker))\b",
            re.IGNORECASE,
        ),
        hints=("circuit breaker", "upstream", "unavailable", "broker"),
    ),
    Fingerprint(
        name="slow_query", category="Performance", severity=25,
        pattern=re.compile(
            r"\bslow quer(?:y|ies)\b|\bquery took (\d{3,})ms\b|\b"
            r"(?:took|exceeded|duration) (\d{3,})ms\b",
            re.IGNORECASE,
        ),
        hints=("slow", "ms", "duration"),
    ),
    Fingerprint(
        name="throttle", category="Resource", severity=42,
        pattern=re.compile(
            r"\btoo many open files\b|\bEMFILE\b|\bENOMEM\b|\bthrottl(?:ed|ing)\b|"
            r"\brate limit(?:ed)? exceeded\b|\b429\b",
            re.IGNORECASE,
        ),
        hints=("open files", "emfile", "enomem", "throttl", "rate limit"),
    ),
    Fingerprint(
        name="corruption", category="Data", severity=55,
        pattern=re.compile(
            r"\b(?:data (?:corruption|corrupted)|checksum mismatch|"
            r"journal file .* is corrupt|malformed packet|unexpected eof)\b",
            re.IGNORECASE,
        ),
        hints=("corrupt", "checksum", "malformed"),
    ),
)


# --------------------------------------------------------------------------- #
# Line format patterns
# --------------------------------------------------------------------------- #

_TS = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"

RE_ISO_HEAD: Final = re.compile(
    rf"^(?P<ts>{_TS})(?P<sep>\s+|\s*\]?\s*:\s*)(?P<rest>.*)$"
)
# Bracketed / parenthesised token at the start of the message, e.g. "[ERROR]".
RE_ENCLOSED: Final = re.compile(r"^\s*(?:\[([^\]]{0,64})\]|\(([^)]{0,64})\)|<([^>]{0,64})>)")
RE_ISO_ONLY: Final = re.compile(rf"^(?P<ts>{_TS})\s*(?P<rest>.*)$")
RE_SYSLOG: Final = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>[\w.\-]+)\s+(?P<tag>[\w.\-/]+?)(?:\[(?P<pid>\d+)\])?:\s*"
    r"(?P<rest>.*)$"
)
RE_COMBINED: Final = re.compile(
    r"^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+"
    r'"(?P<method>[A-Z]+)?\s*(?P<path>[^"]*?)\s*HTTP/(?P<ver>[\d.]+)"\s+'
    r"(?P<status>\d{3})\s+(?P<size>\d+|-)(?:\s+\S+)*$"
)
RE_BRACKET: Final = re.compile(
    r"^\[(?P<ts>[^\]]{4,64})\]\s*\[(?P<level>[A-Za-z]{1,11})\]\s*"
    r"(?P<rest>.*)$"
)
RE_NESTED: Final = re.compile(
    r"^(?P<ts>[A-Za-z]{3}\s+\d{1,2}\s+[\d:]{8})"
    r"(?:[.,]\d+)?\s+\S+\s+\S+?\s+"
    r"\[(?P<level>[A-Za-z]{1,11})\]\s*(?P<rest>.*)$"
)
# Last resort: level token anywhere in the first few tokens.
RE_LOOSE: Final = re.compile(
    r"^(?P<prefix>.*?)(?:[\s\[\(<]|^)(?P<level>"
    r"TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERR|ERROR|CRIT|CRITICAL|FATAL|"
    r"PANIC|ALERT|EMERG)(?:[\s\]\)>:\-]|$)(?P<rest>.*)$",
    re.IGNORECASE,
)
RE_LEVEL_ANYWHERE: Final = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERR|ERROR|CRIT|CRITICAL|"
    r"FATAL|PANIC|ALERT|EMERG)\b"
)
RE_SYSLOG_TS: Final = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>[\d:]{8})"
)
RE_DURATION_MS: Final = re.compile(r"\b(\d{3,})\s*ms\b", re.IGNORECASE)

_LEVEL_PREFIXES: Final = ("[", "(", "<")

_JSON_LEVEL_KEYS: Final = ("level", "severity", "log_level", "loglevel", "lvl",
                            "levelname")
_JSON_MSG_KEYS: Final = ("message", "msg", "log", "event", "error", "err",
                          "text", "description", "detail")
_JSON_TS_KEYS: Final = ("timestamp", "time", "ts", "@timestamp", "datetime",
                        "asctime", "eventtime", "date")

_MONTHS: Final = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TS_FORMATS: Final = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S,%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%d/%b/%Y:%H:%M:%S %z",
    "%b %d %H:%M:%S",
    "%b  %d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d",
)


@lru_cache(maxsize=8192)
def _to_epoch(timestamp: str) -> float:
    """Best-effort conversion of a log timestamp into a Unix epoch.

    Cached aggressively: real logs repeat the same second-level timestamp across
    many lines, and ``strptime``/``fromisoformat`` are by far the most expensive
    part of parsing a batch otherwise.
    """
    if not timestamp:
        return 0.0
    text = timestamp.strip()

    # syslog style has no year -> infer it and assume local time
    syslog = RE_SYSLOG_TS.match(text)
    if syslog:
        month = _MONTHS.get(syslog.group("mon").lower())
        if month:
            year = datetime.now().year
            try:
                stamp = datetime.strptime(
                    f"{year}-{month:02d}-{int(syslog.group('day')):02d} "
                    f"{syslog.group('time')}",
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                return 0.0
            # syslog omits the year; a date ahead of "now" means last year
            if stamp.timestamp() - time.time() > 86_400:
                try:
                    stamp = stamp.replace(year=year - 1)
                except ValueError:
                    return 0.0
            return stamp.timestamp()

    iso = text
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _TS_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        return parsed.timestamp()          # already local
    return parsed.timestamp()


def _level_from_text(text: str) -> Level:
    """Extract a level from free text (defaults to INFO)."""
    match = RE_LEVEL_ANYWHERE.search(text)
    if match is None:
        return Level.INFO
    return parse_level_token(match.group(1))


def _scan_prefix_tokens(line: str, limit: int = 4) -> tuple[Level | None, str]:
    """Scan the first few whitespace tokens for a level keyword.

    Returns ``(level, remainder)`` so the caller can drop the level token from
    the message instead of rendering ``WARN disk full`` as the text.
    """
    tokens = line.split(" ", limit)
    for index, token in enumerate(tokens):
        stripped = token.strip("[]()<>:-,")
        if not stripped:
            continue
        if stripped.isalpha():
            level = lookup_level(stripped)
            if level is not None:
                remainder = " ".join(tokens[index + 1:]).lstrip()
                return level, remainder
        if index >= limit - 1:
            break
    return None, line


class LineParser:
    """Stateless line parser.

    A single instance is shared by the reader, the CLI exporter and the tests;
    it holds only compiled patterns, so it is cheap to create.
    """

    __slots__ = ()

    def parse(
        self,
        line: str,
        source: str = "",
        line_number: int = 0,
    ) -> LogEntry:
        """Parse ``line`` into a fully populated :class:`LogEntry`."""
        stripped = line.strip()
        if not stripped:
            return LogEntry(raw=line, source=source, line_number=line_number)

        entry = (
            self._parse_json(stripped, line, source, line_number)
            if stripped[0] == "{"
            else None
        )
        if entry is None:
            head = line[0]
            if head.isdigit():
                entry = self._parse_iso(line, source, line_number)
            elif head == "[":
                entry = self._parse_bracket(line, source, line_number)
            elif head.isalpha() and head.isascii():
                entry = self._parse_syslog(line, source, line_number)
        if entry is None:
            entry = self._parse_loose(line, source, line_number)

        self._enrich(entry)
        return entry

    # -- format handlers ------------------------------------------------- #

    def _parse_json(self, text: str, line: str, source: str, line_no: int) -> LogEntry | None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None

        level = Level.INFO
        for key in _JSON_LEVEL_KEYS:
            value = data.get(key)
            if value:
                level = Level.from_any(value)
                break
        # numeric syslog severity (RFC 5424): lower is worse
        raw_sev = data.get("severity")
        if isinstance(raw_sev, int) and 0 <= raw_sev <= 7:
            level = {
                0: Level.FATAL, 1: Level.FATAL, 2: Level.CRITICAL,
                3: Level.ERROR, 4: Level.WARN, 5: Level.NOTICE,
                6: Level.INFO, 7: Level.DEBUG,
            }[raw_sev]

        message = ""
        for key in _JSON_MSG_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value:
                message = value
                break
            if value is not None and not isinstance(value, (dict, list)):
                message = str(value)
                break
        if not message:
            message = text

        timestamp = ""
        for key in _JSON_TS_KEYS:
            value = data.get(key)
            if isinstance(value, (str, int, float)):
                timestamp = str(value)
                break

        meta = {
            "host": data.get("host") or data.get("hostname") or "",
            "logger": data.get("logger") or data.get("name") or "",
            "pid": data.get("pid") or data.get("process", {}).get("id")
            if isinstance(data.get("process"), dict) else data.get("pid"),
        }
        meta = {k: v for k, v in meta.items() if v}

        return LogEntry(
            raw=line,
            source=str(meta.get("host") or meta.get("logger") or source),
            level=level,
            timestamp=timestamp,
            message=message,
            line_number=line_no,
            meta=meta,
        )

    def _parse_iso(self, line: str, source: str, line_no: int) -> LogEntry | None:
        combined = RE_COMBINED.match(line)
        if combined is not None:
            return self._from_combined(combined, line, source, line_no)

        head = RE_ISO_HEAD.match(line)
        if head is None:
            return None

        timestamp = head.group("ts")
        rest = head.group("rest")
        level: Level | None = None
        src: str | None = None

        # Consume up to two leading `[...]` / `(...)` / `<...>` tokens. A token
        # that names a known level becomes the level; anything else is the
        # source. This handles both "[ERROR] [api] msg" and "[api] [ERROR] msg".
        for _ in range(2):
            enclosed = RE_ENCLOSED.match(rest)
            if enclosed is None:
                break
            token = next(
                (g for g in enclosed.groups() if g is not None), ""
            ).strip()
            if not token:
                break
            candidate = lookup_level(token)
            if candidate is not None and level is None:
                level = candidate
            elif src is None:
                src = token
            else:
                break
            rest = rest[enclosed.end():].lstrip(" \t:|-\u2013\u2014")

        if level is None:
            level, rest = _scan_prefix_tokens(rest)
            if level is None:
                level = _level_from_text(rest)

        return LogEntry(
            raw=line,
            source=src or source,
            level=level,
            timestamp=timestamp,
            message=rest or line,
            line_number=line_no,
            meta={"src": src} if src else {},
        )

    def _parse_bracket(self, line: str, source: str, line_no: int) -> LogEntry | None:
        match = RE_BRACKET.match(line)
        if match is None:
            match = RE_LOOSE.match(line)
        if match is None:
            return None
        groups = match.groupdict()
        if groups.get("level") is None:
            return None
        prefix = groups.get("prefix") or ""
        return LogEntry(
            raw=line,
            source=source,
            level=parse_level_token(groups["level"]),
            timestamp=(groups.get("ts") or prefix).strip(),
            message=(groups.get("rest") or line).strip(),
            line_number=line_no,
        )

    def _parse_syslog(self, line: str, source: str, line_no: int) -> LogEntry | None:
        match = RE_SYSLOG.match(line)
        if match is None:
            return None
        groups = match.groupdict()
        message = groups["rest"]
        meta = {"host": groups.get("host") or "", "tag": groups.get("tag") or ""}
        if groups.get("pid"):
            meta["pid"] = groups["pid"]
        # Syslog has no level; use the tag prefix, then the message text.
        level, _ = _scan_prefix_tokens(groups["tag"] or "")
        if level is None:
            level, message = _scan_prefix_tokens(message)
        if level is None:
            level = _level_from_text(message)
        return LogEntry(
            raw=line,
            source=groups.get("host") or source,
            level=level,
            timestamp=groups["ts"],
            message=message or line,
            line_number=line_no,
            meta={k: v for k, v in meta.items() if v},
        )

    def _from_combined(self, match: re.Match[str], line: str, source: str,
                       line_no: int) -> LogEntry:
        groups = match.groupdict()
        status = int(groups["status"])
        if status >= 500:
            level = Level.ERROR
        elif status >= 400:
            level = Level.WARN
        elif status >= 300:
            level = Level.INFO
        else:
            level = Level.DEBUG
        path = groups.get("path") or ""
        method = groups.get("method") or ""
        message = f"{method} {path} -> {status}".strip()
        fingerprint = ""
        if status >= 500:
            fingerprint = "http_5xx"
        elif status >= 400:
            fingerprint = "http_4xx"
        return LogEntry(
            raw=line,
            source=source or groups.get("ip") or "",
            level=level,
            timestamp=groups["ts"],
            message=message,
            line_number=line_no,
            fingerprint=fingerprint,
            meta={
                "ip": groups.get("ip"),
                "status_code": status,
                "bytes": None if groups.get("size") == "-" else int(groups["size"]),
                "method": method,
                "path": path,
            },
        )

    def _parse_loose(self, line: str, source: str, line_no: int) -> LogEntry:
        match = RE_LOOSE.match(line)
        if match is not None:
            groups = match.groupdict()
            prefix = (groups.get("prefix") or "").strip()
            return LogEntry(
                raw=line,
                source=source,
                level=parse_level_token(groups["level"]),
                timestamp=prefix[:80],
                message=(groups.get("rest") or line).strip(),
                line_number=line_no,
            )
        return LogEntry(raw=line, source=source, level=_level_from_text(line),
                        timestamp="", message=line, line_number=line_no)

    # -- enrichment ------------------------------------------------------ #

    def _enrich(self, entry: LogEntry) -> None:
        """Attach epoch time, fingerprint and duration extras in one place.

        Classification may only *raise* a level, never lower it: an explicit
        ``[ERROR]`` tag is stronger evidence than a keyword heuristic.
        """
        entry.ts_epoch = _to_epoch(entry.timestamp)

        if not entry.message:
            entry.message = entry.raw

        candidate = entry.message if entry.level >= Level.WARN else entry.raw
        if entry.level >= Level.WARN or _ERROR_PREFILTER.search(candidate):
            level, fingerprint, duration_ms = self.classify(candidate)
            entry.level = max(entry.level, level)
            if fingerprint:
                entry.fingerprint = fingerprint
            if duration_ms is not None:
                entry.meta["duration_ms"] = duration_ms
        else:
            entry.fingerprint = f"ok:{entry.level.label}"

    def classify(self, text: str) -> tuple[Level, str, int | None]:
        """Return ``(level, fingerprint_name, duration_ms)`` for a message.

        The returned level is a *minimum* severity derived from the message; the
        caller combines it with any explicit level parsed from the line.
        """
        level = _level_from_text(text)
        if not _ERROR_PREFILTER.search(text):
            return level, "", _duration_ms(text)

        # ``text.lower()`` is materialised once and shared by every hint check.
        lower = text.lower()
        best_name = ""
        best_severity = -1
        search = _compiled_search

        for fp in _BY_SEVERITY:
            # Severity-descending order means the first full match usually wins
            # and the early exit below skips the rest.
            if fp.severity <= best_severity:
                break
            hints = fp.hints
            if hints is not None:
                for hint in hints:
                    if hint in lower:
                        break
                else:
                    continue
            if search(fp.pattern, text) is None:
                continue
            best_severity = fp.severity
            best_name = fp.name

        if best_name:
            severity_level = Level(max(int(best_severity // 10) * 10,
                                       Level.INFO.value))
            level = max(level, severity_level)

        return level, best_name, _duration_ms(text)


def _duration_ms(text: str) -> int | None:
    if "ms" not in text and "MS" not in text:
        return None
    ms = RE_DURATION_MS.search(text)
    if ms is None:
        return None
    try:
        return int(ms.group(1))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


# Prefilter that must match before we bother running the fingerprint regexes.
_ERROR_PREFILTER = re.compile(
    r"error|exception|fail|fatal|panic|critical|warn|refus|timeout|timed out|"
    r"denied|unauthor|forbidden|deadlock|oom|memory|disk|space|traceback|"
    r"status|slow|unavailable|reset|certificate|ssl|upstream|circuit|"
    r"429|5\d\d|4\d\d",
    re.IGNORECASE,
)

# Severity-descending order enables an early exit in ``LineParser.classify``.
_BY_SEVERITY: Final[tuple[Fingerprint, ...]] = tuple(
    sorted(FINGERPRINTS, key=lambda f: -f.severity)
)

# Bound once: attribute lookup on the hot path is measurable at this volume.
_compiled_search = re.Pattern.search

_DEFAULT_PARSER = LineParser()


def parse_entry(line: str, source: str = "", line_number: int = 0) -> LogEntry:
    """Module-level convenience wrapper around a shared parser instance."""
    return _DEFAULT_PARSER.parse(line, source, line_number)