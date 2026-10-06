"""Realistic demo log generator.

Replaces the original 12-event loop with a scenario-driven generator: a baseline
of healthy traffic punctuated by real incident scenarios, so filters, anomaly
detection and the diagnostics engine are all genuinely exercised.
"""

from __future__ import annotations

import gzip
import math
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import IO

__all__ = ["DEMO_SCENARIOS", "DemoGenerator"]

TS = "%Y-%m-%d %H:%M:%S"

# (weight, template) - weight drives relative frequency
_HEALTHY: tuple[tuple[int, str], ...] = (
    (14, "INFO  [api] GET /api/v1/orders 200 {ms}ms"),
    (12, "INFO  [api] POST /api/v1/checkout 201 {ms}ms"),
    (10, "DEBUG [db] SELECT * FROM orders WHERE id = $1 -- {ms}ms"),
    (9, "INFO  [cache] cache hit key=user:session:{n} ttl=312s"),
    (8, "INFO  [worker] job {n} completed in {ms}ms status=ok"),
    (7, "DEBUG [http] -> 200 GET /healthz from 10.0.0.{d}"),
    (6, "INFO  [api] PATCH /api/v1/users/{n} 200 {ms}ms"),
    (5, "INFO  [queue] published event order.created id={n}"),
    (4, "TRACE [net] tcp keepalive peer=10.0.0.{d} idle=42s"),
)

_WARN_NOISY: tuple[tuple[int, str], ...] = (
    (6, "WARN  [cache] slow get key=user:profile:{n} took {ms}ms"),
    (5, "WARN  [db] connection pool at 85% capacity (17/20)"),
    (4, "WARN  [api] slow query detected: {ms}ms in list_orders()"),
    (3, "WARN  [net] client 10.0.0.{d} rate limited (429)"),
)

# Scenario: burst of errors that should trip anomaly detection.
DEMO_SCENARIOS: tuple[dict[str, object], ...] = (
    {
        "name": "database deadlock storm",
        "weight": 6,
        "lines": (
            "ERROR [db] ERROR: deadlock detected while waiting for lock on orders",
            "ERROR [db] ERROR: deadlock found when trying to get lock; retrying txn {n}",
            "ERROR [api] 500 Internal Server Error: transaction aborted at commit",
            "WARN  [db] statement timeout after {ms}ms on relation orders_pkey",
            "ERROR [db] connection pool exhausted (max=20)",
        ),
    },
    {
        "name": "redis outage",
        "weight": 5,
        "lines": (
            "ERROR [cache] Connection refused: Unable to connect to redis://127.0.0.1:6379",
            "WARN  [cache] circuit breaker OPEN for redis",
            "ERROR [cache] Connection refused: Unable to connect to redis://127.0.0.1:6379",
            "ERROR [api] 503 Service Unavailable: no healthy upstream (cache)",
        ),
    },
    {
        "name": "memory pressure / OOM",
        "weight": 3,
        "lines": (
            "CRITICAL [kernel] Out of memory: OOMKilled process {n} (node)",
            "ERROR [runtime] MemoryError: cannot allocate 4194304 bytes",
            "WARN  [runtime] memory usage 94% heap=7.8G rss=8.4G",
        ),
    },
    {
        "name": "certificate expiry",
        "weight": 2,
        "lines": (
            "ERROR [http] SSL handshake failed: certificate verify failed: "
            "unable to get local issuer certificate",
            "ERROR [api] upstream auth: TLS error while calling payments-gateway",
        ),
    },
    {
        "name": "python traceback",
        "weight": 3,
        "lines": (
            "ERROR [worker] Traceback (most recent call last):",
            "ERROR [worker]   File \"/app/orders/processor.py\", line 214, in handle",
            "ERROR [worker] ValueError: invalid literal for int() with base 10: 'abc-{n}'",
        ),
    },
    {
        "name": "slow query",
        "weight": 4,
        "lines": (
            "WARN  [db] slow query detected: {ms}ms in get_user_dashboard()",
            "WARN  [db] slow query detected: {ms}ms in list_orders_by_status()",
            "ERROR [db] statement timeout cancelling query after {ms}ms",
        ),
    },
    {
        "name": "permission / disk",
        "weight": 2,
        "lines": (
            "ERROR [fs] Permission denied: open '/etc/smartlog/secrets.yaml'",
            "ERROR [fs] No space left on device writing /var/lib/smartlog/index.db",
            "CRITICAL [fs] disk full: 100% used on /var",
        ),
    },
    {
        "name": "auth failures",
        "weight": 2,
        "lines": (
            "ERROR [auth] authentication failed for user admin from 203.0.113.{d}",
            "WARN  [auth] invalid token presented by client 203.0.113.{d}",
            "ERROR [auth] login failed: too many failed attempts for user svc-{n}",
        ),
    },
)

_JSON_LINES: tuple[tuple[int, str], ...] = (
    (10, '{"ts":"{ts}","level":"INFO","logger":"api","msg":"GET /v1/health 200",'
        '"request_id":"r-{n}"}'),
    (5, '{"ts":"{ts}","level":"ERROR","logger":"api","msg":"upstream timeout",'
        '"request_id":"r-{n}","status":504}'),
    (3, '{"ts":"{ts}","level":"WARN","logger":"pool","msg":"pool saturation 90%",'
        '"waiters":7}'),
)


def _fill(template: str, rng: random.Random) -> str:
    """Substitute the demo placeholders.

    ``str.format`` is unusable here because the templates include JSON lines
    whose braces must survive untouched, so tokens are replaced literally.
    """
    if "{" not in template:
        return template
    replacements = {
        "{ts}": datetime.now().strftime(TS),
        "{n}": str(rng.randint(1000, 99999)),
        "{d}": str(rng.randint(2, 250)),
        "{ms}": str(
            rng.randint(1, 900) if rng.random() < 0.85 else rng.randint(1000, 6000)
        ),
    }
    for token, value in replacements.items():
        if token in template:
            template = template.replace(token, value)
    return template


class DemoGenerator:
    """Writes a plausible log stream to a real file, so tailing is genuinely tested.

    Supports periodic log rotation (rename + fresh file) and gzip compression of
    the rotated file, which is the most common real-world rotation pattern.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        base_rate: float = 12.0,
        burst: bool = True,
        rotate_every: int = 400,
        gzip_rotated: bool = True,
        seed: int | None = None,
        interval: float = 0.12,
        min_rotate_gap: float = 1.0,
    ) -> None:
        if directory is None:
            base = Path(os.environ.get("TMPDIR", "/tmp")) / "smartlog-demo"
        else:
            base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        self.directory = base
        self.path = base / "app.log"
        self.base_rate = base_rate
        self.burst = burst
        self.rotate_every = rotate_every
        self.gzip_rotated = gzip_rotated
        self.interval = interval
        self.min_rotate_gap = min_rotate_gap
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._written = 0
        self._rotations = 0
        self._last_rotation = 0.0
        self._handle: IO[str] | None = None

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> Path:
        self._open()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="SmartLogDemoWriter", daemon=True
        )
        self._thread.start()
        return self.path

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    @property
    def lines_written(self) -> int:
        return self._written

    @property
    def rotations(self) -> int:
        return self._rotations

    # -- internals -------------------------------------------------------- #

    def _open(self) -> None:
        self._handle = open(self.path, "a", encoding="utf-8")
        stamp = datetime.now().strftime(TS)
        self._handle.write(
            f"{stamp} INFO  [system] demo stream ready "
            f"(dir={self.directory})\n"
        )
        self._handle.flush()

    def _run(self) -> None:
        rng = self._rng
        healthy = list(_HEALTHY) * 6
        warn = list(_WARN_NOISY) * 4
        json_lines = list(_JSON_LINES) * 3
        scenario_pools = list(DEMO_SCENARIOS) * 3

        while not self._stop.is_set():
            rng.random()

            # 12% chance of an incident burst of 4-14 lines.
            if self.burst and rng.random() < 0.12:
                scenario = rng.choice(scenario_pools)
                count = rng.randint(4, 14)
                templates: list[str] = scenario["lines"]  # type: ignore[assignment]
                block = [
                    f"{datetime.now().strftime(TS)} "
                    f"{_fill(rng.choice(templates), rng)}\n"
                    for _ in range(count)
                ]
                self._emit("".join(block))
            else:
                pool = rng.choice((healthy, healthy, warn, json_lines))
                line = _fill(rng.choice(pool)[1], rng)
                # Plaintext lines carry their own prefix; JSON lines start with '{'.
                if line.startswith("{"):
                    self._emit(line + "\n")
                else:
                    self._emit(f"{datetime.now().strftime(TS)} {line}\n")

            # Exponential inter-arrival time (Poisson process) around the rate.
            delay = self.interval / max(self.base_rate, 0.1) * -math.log(
                1.0 - rng.random()
            )
            self._stop.wait(min(max(delay, 0.0), 2.0))

    def _emit(self, payload: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(payload)
            self._handle.flush()
            self._written += payload.count("\n")
            if self.rotate_every and self._written >= self.rotate_every:
                self._maybe_rotate()
        except OSError:
            pass

    def _maybe_rotate(self) -> None:
        """Rotate at most once per ``min_rotate_gap`` seconds.

        Rotating on a pure line count is fine for a demo, but at a high base
        rate it would mean hundreds of gzip passes per second and the generator
        would measure its own compression instead of the reader.
        """
        now = time.monotonic()
        if now - self._last_rotation < self.min_rotate_gap:
            return
        self._last_rotation = now
        self._rotate()

    def _rotate(self) -> None:
        """Close, rename, optionally gzip, then reopen — exercises rotation."""
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
        rotated = self.directory / f"app.log.{stamp}"
        try:
            os.replace(self.path, rotated)
            self._rotations += 1
        except OSError:
            self._written = 0
            self._open()
            return
        if self.gzip_rotated:
            try:
                with open(rotated, "rb") as src, gzip.open(
                    f"{rotated}.gz", "wb", compresslevel=1
                ) as dst:
                    dst.writelines(src)
                os.remove(rotated)
            except OSError:
                pass
        self._written = 0
        self._open()


def main() -> None:  # pragma: no cover - manual entry point
    gen = DemoGenerator()
    print(f"writing demo logs to {gen.start()} - press Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        gen.stop()
        print(f"\nwrote {gen.lines_written} lines")


if __name__ == "__main__":  # pragma: no cover
    main()