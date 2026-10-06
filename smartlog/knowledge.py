"""Offline rule-based error knowledge base.

Every entry is a curated rule: match -> explanation -> concrete, runnable fix
commands -> prevention guidance. This is the *last* fallback when no LLM is
reachable, and it is also used to pre-classify errors before spending LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["RULES", "KnowledgeBase", "Rule", "match_rules"]


@dataclass(frozen=True, slots=True)
class Rule:
    """One diagnostic rule."""

    id: str
    title: str
    category: str
    root_cause: str
    confidence: str                       # low | medium | high | very_high
    patterns: tuple[re.Pattern[str], ...]
    checks: tuple[tuple[str, str, str], ...] = ()   # (title, description, command)
    prevention: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    hints: tuple[str, ...] | None = None
    priority: int = 0                     # tie-breaker when scores are equal


def _rx(*alts: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(alt, re.IGNORECASE) for alt in alts)


def _c(title: str, desc: str, cmd: str = "") -> tuple[str, str, str]:
    return (title, desc, cmd)


RULES: tuple[Rule, ...] = (
    Rule(
        id="oom",
        title="Out of memory / OOM kill",
        category="Resource",
        root_cause=(
            "The process exceeded its memory limit (or the host ran out of RAM) "
            "and was terminated by the kernel / runtime."
        ),
        confidence="very_high",
        patterns=_rx(
            r"\bout of memory\b", r"\boom-?kill(?:ed)?\b",
            r"\bMemoryError\b", r"\bcannot allocate memory\b",
            r"\bJavaScript heap out of memory\b",
        ),
        hints=("memory", "oom", "allocat"),
        checks=(
            _c("Identify memory hogs",
               "Sort processes by resident memory.",
               "ps aux --sort=-%mem | head -15"),
            _c("Check host memory",
               "Verify free vs available memory and swap pressure.",
               "free -h && swapon --show"),
            _c("Inspect container limits",
               "Compare the container limit with actual usage.",
               "docker stats --no-stream && docker inspect <ctr> | grep -i memory"),
            _c("Check for a leak signature",
               "Repeated growth across restarts usually means a leak, not a spike.",
               "journalctl -u <service> --since '1 hour ago' | grep -i -E 'oom|memory'"),
        ),
        prevention=(
            "Set explicit memory limits and alert at 80% of them",
            "Run a heap profiler on the workload before scaling up",
            "Stream or chunk large datasets instead of materialising them",
        ),
        docs=("https://docs.kernel.org/admin-guide/mm/ksm.html",),
    ),
    Rule(
        id="disk_full",
        title="No space left on device",
        category="Resource",
        root_cause="The filesystem reached 100% usage, so writes now fail.",
        confidence="very_high",
        patterns=_rx(r"\bno space left on device\b", r"\bENOSPC\b",
                     r"\bdisk (?:is )?full\b"),
        hints=("space", "disk", "enospc"),
        checks=(
            _c("Find the full filesystem", "Check inode usage too, not just blocks.",
               "df -h && df -i"),
            _c("Locate the biggest consumers", "Walk one level at a time.",
               "du -xh / --max-depth=2 2>/dev/null | sort -h | tail -20"),
            _c("Vacuum journald", "Often the quickest safe win on systemd hosts.",
               "sudo journalctl --vacuum-size=200M"),
            _c("Rotate application logs", "Confirm logrotate runs.",
               "sudo logrotate -f /etc/logrotate.conf"),
        ),
        prevention=(
            "Configure logrotate for every log-emitting service",
            "Alert at >80% filesystem usage",
            "Reserve capacity for tmpfs / swap",
        ),
    ),
    Rule(
        id="db_deadlock",
        title="Database deadlock / lock contention",
        category="Database",
        root_cause=(
            "Two or more transactions acquired the same rows in a different order "
            "(or held locks too long), so the engine aborted one of them."
        ),
        confidence="very_high",
        patterns=_rx(
            r"\bdeadlock detected\b", r"\block wait timeout\b",
            r"\bdeadlock found when trying to get lock\b",
            r"\b(?:connection|pool) exhausted\b",
        ),
        hints=("deadlock", "lock wait", "exhausted"),
        priority=5,
        checks=(
            _c("Capture the deadlock report", "Postgres logs the full cycle.",
               "journalctl -u postgresql | grep -A20 -i deadlock"),
            _c("Show blocking statements", "Run inside psql.",
               "SELECT pid, wait_event_type, wait_event, query, now()-query_start "
               "AS runtime FROM pg_stat_activity WHERE state <> 'idle' ORDER BY "
               "runtime DESC;"),
            _c("Confirm autovacuum health", "Stale stats inflate deadlock risk.",
               "SELECT relname, n_dead_tup, last_autovacuum FROM pg_stat_user_tables "
               "ORDER BY n_dead_tup DESC LIMIT 10;"),
            _c("Show lock graph", "In MySQL:",
               "SHOW ENGINE INNODB STATUS\\G"),
        ),
        prevention=(
            "Always acquire row locks in a consistent (alphabetical) order",
            "Keep transactions short; never call external services inside them",
            "Add retry with jitter for SQLSTATE 40001 / error 1213",
            "Add covering indexes for the ORDER BY / WHERE columns",
        ),
        docs=("https://www.postgresql.org/docs/current/explicit-locking.html",
              "https://www.postgresql.org/docs/current/monitoring-locks.html"),
    ),
    Rule(
        id="conn_refused",
        title="Connection refused",
        category="Availability",
        root_cause=(
            "Nothing is listening on the target host/port, the service is down, "
            "or a firewall rejected the SYN with RST."
        ),
        confidence="high",
        patterns=_rx(r"\bconnection refused\b", r"\bECONNREFUSED\b",
                     r"\bno route to host\b", r"\bconnection reset by peer\b",
                     r"\bECONNRESET\b", r"\bno healthy upstream\b"),
        hints=("refused", "econn", "route to host", "reset"),
        checks=(
            _c("Is the process alive?", "", "systemctl status <service>"),
            _c("Is it listening?", "Compare with the port you dial.",
               "ss -tlnp | grep <port>"),
            _c("Probe locally", "",
               "curl -sv http://127.0.0.1:<port>/health"),
            _c("Check the firewall", "", "sudo ufw status verbose"),
            _c("Confirm the DNS target", "A stale record points at the wrong host.",
               "getent hosts <hostname>"),
        ),
        prevention=(
            "Declare dependencies with systemd After=/Requires=",
            "Health-check dependencies before serving traffic",
            "Use exponential backoff with jitter on connect",
        ),
    ),
    Rule(
        id="timeout",
        title="Timeout",
        category="Performance",
        root_cause=(
            "An operation exceeded its deadline. Either the peer is slow/stuck, or "
            "the timeout budget is simply too tight for the workload."
        ),
        confidence="high",
        patterns=_rx(r"\btimed? ?out\b", r"\bETIMEDOUT\b",
                     r"\bdeadline exceeded\b",
                     r"\breadiness probe failed\b",
                     r"\bcontext deadline\b"),
        hints=("timed out", "timeout", "etimedout", "deadline"),
        checks=(
            _c("Measure the baseline latency", "",
               "curl -o /dev/null -s -w '%{time_total}\\n' http://<host>/<path>"),
            _c("Look at downstream latency", "Top talkers and stall time.",
               "ss -tnp state established '( sport = :<port> )'"),
            _c("Check pool saturation", "Connections > pool size = self-inflicted wait.",
               "ss -tn state established | wc -l"),
        ),
        prevention=(
            "Set timeouts from measured p99 latency, not guesses",
            "Separate connect / read / write deadlines",
            "Always bound retries with a circuit breaker",
        ),
    ),
    Rule(
        id="permission",
        title="Permission denied",
        category="Permission",
        root_cause="The effective UID lacks the required rights on the target path.",
        confidence="very_high",
        patterns=_rx(r"\bpermission denied\b", r"\bEACCES\b", r"\bEPERM\b",
                     r"\baccess denied\b"),
        hints=("permission", "denied", "eacces", "eperm"),
        checks=(
            _c("Inspect the target", "Check owner, group and the mode bits.",
               "ls -la <path> && namei -l <path>"),
            _c("Check the running identity", "",
               "ps -o user,group,cmd -p $(pidof <proc>)"),
            _c("Check ACLs / SELinux", "Plain modes are not always the culprit.",
               "getfacl <path>; getenforce; ausearch -m avc -ts recent"),
        ),
        prevention=(
            "Create a dedicated service account with least privilege",
            "Set a sane umask (0022) and document ownership",
            "Prefer group membership over per-file chmod",
        ),
    ),
    Rule(
        id="auth_failure",
        title="Authentication failure",
        category="Security",
        root_cause=(
            "Credentials were rejected, the token expired/revoked, the signing "
            "secret or issuer does not match, or the clock is skewed."
        ),
        confidence="high",
        patterns=_rx(
            r"\bauthentication fail(?:ed|ure)\b",
            r"\binvalid (?:credentials|token|password|api key|signature)\b",
            r"\b(?:failed|invalid|incorrect|wrong) (?:password|credentials|"
            r"username|login)\b",
            r"\blogin failed\b", r"\btoken (?:expired|revoked)\b",
            r"\bunauthorized\b", r"\b401\b.{0,20}\b",
        ),
        hints=("auth", "credential", "token", "login", "password", "unauthor", "401"),
        checks=(
            _c("Check clock skew", "JWT validation fails when the clock drifts.",
               "timedatectl status"),
            _c("Validate the issuer/audience", "",
               "python -c \"import jwt,os;print(jwt.decode(os.environ['TOKEN'],"
               "options={'verify_signature':False},verify_aud=False))\""),
            _c("Confirm secret configuration", "",
               "systemctl show <service> -p Environment | tr ' ' '\\n' | grep -i secret"),
            _c("Review auth audit logs", "",
               "journalctl -u <auth-service> --since '10 min ago'"),
        ),
        prevention=(
            "Rotate secrets on a schedule and support key rotation without downtime",
            "Never log tokens, even truncated",
            "Track failed-login rates to detect credential stuffing",
        ),
    ),
    Rule(
        id="tls",
        title="TLS / certificate failure",
        category="Security",
        root_cause=(
            "The certificate chain, hostname, validity window or trusted CA store "
            "did not validate."
        ),
        confidence="high",
        patterns=_rx(
            r"\bcertificate verify failed\b", r"\bx509\b",
            r"\bself[- ]signed certificate\b",
            r"\bunable to get local issuer certificate\b",
            r"\bssl handshake\b", r"\bcertificate has expired\b",
        ),
        hints=("certificate", "x509", "ssl", "tls", "issuer"),
        checks=(
            _c("Inspect the served chain", "",
               "openssl s_client -connect <host>:<port> -servername <host> "
               "-showcerts </dev/null"),
            _c("Check validity and SAN", "Hostname must appear in SAN, not CN.",
               "openssl x509 -in <cert> -noout -dates -ext subjectAltName"),
            _c("Check the local trust store", "Expired roots break everything.",
               "ls /etc/ssl/certs/ca-certificates.crt && update-ca-certificates"),
            _c("Check the clock again", "notBefore/notAfter are clock-sensitive.",
               "timedatectl status"),
        ),
        prevention=(
            "Automate renewal and reload (certbot + deploy hook)",
            "Pin the CA bundle in containers instead of relying on the host",
            "Alert when a certificate is within 30 days of expiry",
        ),
    ),
    Rule(
        id="http_5xx",
        title="HTTP 5xx server error",
        category="HTTP",
        root_cause=(
            "The server failed while handling the request. The 5xx is a symptom; "
            "the real cause is usually an unhandled exception, a failed dependency "
            "or an exhausted resource."
        ),
        confidence="medium",
        patterns=_rx(
            r"\bstatus(?:_code)?\W{0,3}5\d{2}\b",
            r"\bHTTP\W{0,3}5\d{2}\b",
            r"\b5\d{2}\s+(?:internal server error|bad gateway|"
            r"service unavailable|gateway time-?out)\b",
            r"\b(?:Internal Server Error|Bad Gateway)\b",
        ),
        hints=("5", "status", "gateway", "internal server error"),
        checks=(
            _c("Correlate with the application log", "Same request id / trace id.",
               "grep -R '<request-id>' /var/log/app/"),
            _c("Check every downstream dependency", "",
               "systemctl --failed; for p in 5432 6379 9092; do nc -z localhost $p "
               "&& echo \"$p up\" || echo \"$p DOWN\"; done"),
            _c("Read the reverse proxy error log", "Upstream vs client distinction.",
               "tail -n 200 /var/log/nginx/error.log"),
            _c("Check worker saturation", "", "top -bn1 | head -20"),
        ),
        prevention=(
            "Return a structured error body with a correlation id",
            "Add a global exception handler / middleware",
            "Load-test the endpoints that return 5xx under partial failure",
        ),
    ),
    Rule(
        id="http_4xx",
        title="HTTP 4xx client error",
        category="HTTP",
        root_cause=(
            "The request was malformed, unauthorised, or referenced a missing "
            "resource. Client-side input, not server health."
        ),
        confidence="medium",
        patterns=_rx(
            r"\bstatus(?:_code)?\W{0,3}4\d{2}\b",
            r"\bHTTP\W{0,3}4\d{2}\b",
            r"\b4\d{2}\s+(?:bad request|unauthorized|forbidden|not found|"
            r"too many requests)\b",
        ),
        hints=("4", "status", "not found", "bad request"),
        checks=(
            _c("Bucket by status and path", "A 404 storm is usually a bad deploy.",
               "awk '{print $9}' access.log | sort | uniq -c | sort -rn | head"),
            _c("Separate 401/403 from 404", "Auth vs existence leak.",
               "grep -E '\" (401|403|404) ' access.log | head -50"),
            _c("Check rate limiting", "429 often means a client loop.",
               "grep -c ' 429 ' access.log"),
        ),
        prevention=(
            "Validate input at the edge and return precise 4xx codes",
            "Version the API so old clients get a clear deprecation path",
        ),
    ),
    Rule(
        id="circuit_breaker",
        title="Circuit breaker open / dependency down",
        category="Availability",
        root_cause=(
            "The client stopped calling a failing dependency to protect itself. The "
            "root cause lives in the dependency's own logs."
        ),
        confidence="high",
        patterns=_rx(
            r"\bcircuit breaker (?:open|half[- ]open|closed)\b",
            r"\bno healthy upstream\b", r"\bservice unavailable\b",
            r"\bbroker (?:is )?unavailable\b",
            r"\b(?:connection to )?broker .* refused\b",
        ),
        hints=("circuit", "breaker", "upstream", "broker", "unavailable"),
        checks=(
            _c("Find the dependency's health", "",
               "curl -sS http://<dep>:<port>/health | jq ."),
            _c("Check network reachability", "", "nc -vz <dep> <port>"),
            _c("Check the breaker configuration", "Excessive thresholds cause flapping.",
               "grep -R 'circuit' /etc/<service>/"),
        ),
        prevention=(
            "Size the pool from the dependency's real capacity",
            "Ship fallback responses so the breaker path degrades gracefully",
            "Alert on breaker state transitions, not just on open",
        ),
    ),
    Rule(
        id="traceback",
        title="Unhandled exception / stack trace",
        category="Exception",
        root_cause=(
            "An exception escaped the handler. The exception type and the deepest "
            "application frame in the traceback identify the failing operation."
        ),
        confidence="medium",
        patterns=_rx(
            r"\bTraceback \(most recent call last\)",
            r"\b[A-Za-z_][\w.]*(?:Error|Exception)\b:",
            r"\b(?:java\.|org\.|com\.)\w*(?:Exception|Error)\b",
        ),
        hints=("traceback", "error", "exception"),
        checks=(
            _c("Extract the deepest frames", "The top-most project frame is the culprit.",
               "grep -A40 'Traceback' app.log | head -50"),
            _c("Reproduce against the same input", "",
               "python -c \"import json,sys;print(json.load(open('payload.json')))\""),
            _c("Look for a recent change", "",
               "git log --oneline -20 -- <suspect path>"),
        ),
        prevention=(
            "Catch broad exceptions only at process boundaries and log with context",
            "Attach request/correlation ids to every error record",
            "Fail the build on new unhandled-exception fingerprints",
        ),
    ),
    Rule(
        id="panic",
        title="Process panic / crash",
        category="Process",
        root_cause=(
            "The runtime aborted. Corelimit, a failed assertion, an out-of-bounds "
            "access or an unrecoverable fault — the core dump holds the answer."
        ),
        confidence="very_high",
        patterns=_rx(
            r"\bpanic:|\bfatal error\b", r"\bsegmentation fault\b",
            r"\bcore dumped\b", r"\babort(?:ed)?\b", r"\bSIGSEGV\b",
        ),
        hints=("panic", "fatal", "segfault", "core", "abort", "sigsegv"),
        checks=(
            _c("Read the kernel view", "Signals delivered to the process.",
               "dmesg -T | tail -40"),
            _c("Check the cgroup/OOM interaction", "Killed vs crashed is not the same.",
               "systemctl status <service>"),
            _c("Enable core dumps", "", "ulimit -c unlimited; cat /proc/sys/kernel/core_pattern"),
        ),
        prevention=(
            "Recreate crashes in a canary environment with cores enabled",
            "Guard every unchecked slice/pointer access at boundaries",
        ),
    ),
    Rule(
        id="slow_query",
        title="Slow database query",
        category="Performance",
        root_cause=(
            "A statement exceeded its latency budget: missing index, bad plan, lock "
            "wait, or simply too much data returned."
        ),
        confidence="medium",
        patterns=_rx(
            r"\bslow quer(?:y|ies)\b", r"\bquery took \d{3,}ms\b",
            r"\bstatement timeout\b", r"\b\d{3,} ?ms\b.{0,30}\bquery\b",
        ),
        hints=("slow", "query", "ms", "timeout"),
        checks=(
            _c("Capture the plan (Postgres)", "", "EXPLAIN (ANALYZE, BUFFERS) <query>;"),
            _c("Find the worst offenders", "",
               "SELECT query, calls, mean_exec_time FROM pg_stat_statements "
               "ORDER BY total_exec_time DESC LIMIT 10;"),
            _c("Check index usage and bloat", "",
               "SELECT relname, idx_scan, idx_tup_read FROM pg_stat_user_indexes "
               "ORDER BY idx_scan ASC LIMIT 10;"),
            _c("Locks blocking the query", "",
               "SELECT * FROM pg_locks WHERE NOT granted;"),
        ),
        prevention=(
            "Add a composite index matching the WHERE + ORDER BY columns",
            "Set a per-statement timeout so regressions fail fast",
            "Track query fingerprints in CI against a performance budget",
        ),
    ),
    Rule(
        id="file_not_found",
        title="Missing file / resource",
        category="IO",
        root_cause="The path does not exist, or the process cannot see it (cwd/namespace).",
        confidence="high",
        patterns=_rx(
            r"\bFileNotFoundError\b", r"\bNo such file or directory\b",
            r"\bENOENT\b", r"\bErrNotExist\b", r"\b404 not found\b",
        ),
        hints=("not found", "enoent", "nosuchfile", "filenotfound"),
        checks=(
            _c("Resolve every path component", "Catches permission issues too.",
               "namei -l /full/path/to/file"),
            _c("Confirm the working directory", "",
               "readlink -f . && pwd -P"),
            _c("Check the mount / namespace", "Container may not mount the volume.",
               "findmnt -T /full/path && ls -la /full/path"),
        ),
        prevention=(
            "Validate configuration paths at startup, not at first use",
            "Fail fast with an explicit startup self-check",
        ),
    ),
    Rule(
        id="corruption",
        title="Data corruption",
        category="Data",
        root_cause=(
            "On-disk or in-transit data failed its integrity check. This is a "
            "severity-1 incident: stop writes and preserve evidence."
        ),
        confidence="high",
        patterns=_rx(
            r"\bdata corrupt(?:ion|ed)\b", r"\bchecksum mismatch\b",
            r"\bis corrupt\b", r"\bmalformed packet\b", r"\bCRC\b.{0,10}\berror\b",
        ),
        hints=("corrupt", "checksum", "malformed", "crc"),
        priority=8,
        checks=(
            _c("Freeze writes first", "Every write may spread the damage.",
               "systemctl stop <service>"),
            _c("Snapshot the evidence", "",
               "sudo cp -a /var/lib/<app> /var/tmp/<app>-$(date +%s)"),
            _c("Check storage health", "",
               "sudo dmesg -T | grep -i -E 'medium error|uncorrectable'"),
        ),
        prevention=(
            "Enable end-to-end checksums and verify them on read",
            "Maintain tested, restorable backups",
            "Monitor SMART attributes and NVMe error counters",
        ),
    ),
    Rule(
        id="throttle",
        title="Rate limited / resource limit",
        category="Resource",
        root_cause="A quota or file-descriptor limit was hit.",
        confidence="high",
        patterns=_rx(
            r"\btoo many open files\b", r"\bEMFILE\b", r"\b429\b",
            r"\brate limit(?:ed)? exceeded\b", r"\bthrottl(?:ed|ing)\b",
            r"\bquota exceeded\b",
        ),
        hints=("too many", "emfile", "429", "rate limit", "throttl", "quota"),
        checks=(
            _c("Check the fd limit and usage", "",
               "ulimit -n; ls /proc/$(pidof <proc>)/fd | wc -l"),
            _c("Find the leaked descriptors", "",
               "ls -l /proc/$(pidof <proc>)/fd | awk '{print $NF}' | sort | uniq -c "
               "| sort -rn | head"),
            _c("Identify the client looping", "",
               "grep ' 429 ' access.log | awk '{print $1}' | sort | uniq -c | sort -rn"),
        ),
        prevention=(
            "Close resources with context managers; stream instead of buffering",
            "Apply exponential backoff plus jitter on client retries",
            "Expose quota headers so clients can self-throttle",
        ),
    ),
    Rule(
        id="dns",
        title="DNS resolution failure",
        category="Network",
        root_cause="The name could not be resolved: NXDOMAIN, no resolver, or a resolver timeout.",
        confidence="high",
        patterns=_rx(
            r"\bNXDOMAIN\b", r"\bname or service not known\b",
            r"\btemporary failure in name resolution\b",
            r"\bdns resolution failed\b", r"\bno such host\b",
        ),
        hints=("dns", "resolve", "nxdomain", "no such host"),
        checks=(
            _c("Query the resolvers directly", "",
               "dig +short <name> @1.1.1.1; nslookup <name>"),
            _c("Check the resolver config", "",
               "cat /etc/resolv.conf && resolvectl status 2>/dev/null | head -30"),
            _c("Test both transports", "", "ping -c2 <name>; curl -sv https://<name>/ 2>&1 | head"),
        ),
        prevention=(
            "Cache DNS results with a TTL-aware resolver",
            "Monitor resolution latency and failure rate separately",
        ),
    ),
    Rule(
        id="graceful_restart",
        title="Crash loop / restart storm",
        category="Process",
        root_cause=(
            "A supervisor keeps restarting a crashing process. Each restart loses "
            "in-flight work, so the crash loop is the incident."
        ),
        confidence="high",
        patterns=_rx(
            r"\bstart request repeated too quickly\b",
            r"\bScheduled restart job\b",
            r"\bRestarting \w+ service\b",
            r"\bBack-off restarting\b", r"\bfailed with result\b",
        ),
        hints=("restart", "back-off", "repeated too quickly"),
        checks=(
            _c("List failed units", "", "systemctl --failed"),
            _c("Read the unit's own log", "", "journalctl -u <unit> -n 100 --no-pager"),
            _c("Count restarts", "A rising count means an unresolved crash.",
               "systemctl show <unit> -p NRestarts"),
        ),
        prevention=(
            "Raise StartLimit only after fixing the underlying crash",
            "Add a health gate so restarts wait for readiness",
        ),
    ),
)


class KnowledgeBase:
    """Rule lookup with specificity scoring.

    Scoring favours the longest literal match (more specific evidence) and uses
    ``priority`` as a tie-breaker, which is far more stable than the original
    "longest matched substring wins" heuristic.
    """

    __slots__ = ("_prepared", "_rules")

    def __init__(self, rules: tuple[Rule, ...] = RULES) -> None:
        self._rules = rules

    def lookup(self, text: str, *, limit: int = 3) -> list[tuple[Rule, str]]:
        """Return up to ``limit`` ``(rule, matched_text)`` pairs, best first."""
        lowered = text.lower()
        hits: list[tuple[int, int, Rule, str]] = []
        for rule in self._rules:
            if rule.hints is not None and not any(h in lowered for h in rule.hints):
                continue
            best: tuple[int, str] | None = None
            for pattern in rule.patterns:
                match = pattern.search(text)
                if match is None:
                    continue
                score = len(match.group(0))
                if best is None or score > best[0]:
                    best = (score, match.group(0))
            if best is not None:
                hits.append((best[0] + rule.priority * 100, best[0], rule, best[1]))
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
        return [(rule, matched) for _, _, rule, matched in hits[:limit]]

    def best(self, text: str) -> tuple[Rule, str] | None:
        found = self.lookup(text, limit=1)
        return found[0] if found else None

    def category_for(self, text: str) -> str:
        hit = self.best(text)
        return hit[0].category if hit else "General"


_DEFAULT = KnowledgeBase()


def match_rules(text: str, limit: int = 3) -> list[tuple[Rule, str]]:
    """Module-level convenience wrapper around the shared knowledge base."""
    return _DEFAULT.lookup(text, limit=limit)