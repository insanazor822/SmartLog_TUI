#!/usr/bin/env python3
"""Benchmark: parse throughput, filter throughput and export cost.

Run against the legacy implementation to get a side-by-side number:

    python benchmarks/bench.py --lines 200000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartlog import (
    Exporter,
    FilterEngine,
    LogStats,
    build_spec,
    parse_entry,
)

TEMPLATES = [
    "2026-08-30T20:00:01.123Z [INFO] [api] GET /api/v1/orders 200 {ms}ms",
    "2026-08-30T20:00:02.456Z [DEBUG] [db] SELECT * FROM orders WHERE id = $1 -- {ms}ms",
    "2026-08-30T20:00:03.789Z [WARN] [db] slow query detected: {ms}ms in get_user()",
    "2026-08-30T20:00:04.001Z [ERROR] [db] deadlock detected while waiting for lock",
    "2026-08-30T20:00:05.111Z [CRITICAL] Out of memory: OOMKilled process {n}",
    "2026-08-30T20:00:06.222Z [ERROR] [cache] Connection refused: redis://127.0.0.1:6379",
    "2026-08-30T20:00:07.333Z [INFO] [worker] job {n} completed in {ms}ms",
    '2026-08-30T20:00:08.444Z {{"level":"error","msg":"pool exhausted","host":"web-01"}}',
    "Aug 30 20:00:09 web01 sshd[812]: Failed password for root from 203.0.113.7",
    '203.0.113.9 - - [30/Aug/2026:20:00:10 +0000] "GET /api/v1/x HTTP/1.1" 500 120 "-" "curl/8"',
]


def make_lines(count: int) -> list[str]:
    lines = []
    for i in range(count):
        template = TEMPLATES[i % len(TEMPLATES)]
        lines.append(template.format(n=i, ms=10 + (i % 4000)))
    return lines


def timeit(label: str, fn, *, rounds: int = 1) -> float:
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    elapsed = time.perf_counter() - start
    size = f"x{rounds}" if rounds > 1 else ""
    print(f"{label:<44} {elapsed * 1000:9.1f} ms {size}")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=200_000)
    args = parser.parse_args()

    lines = make_lines(args.lines)
    payload = len("\n".join(lines))
    print("\nsmartlog benchmark")
    print(f"{args.lines:,} lines / {payload / 1e6:.1f} MB")
    print("-" * 62)

    # ---- parsing ------------------------------------------------------- #
    def parse_all() -> list:
        return [parse_entry(line, "app.log", i) for i, line in enumerate(lines)]

    t_parse = timeit("parse (mixed formats)", parse_all)
    entries = parse_all()
    print(f"{'':44} {len(entries) / t_parse:9,.0f} lines/s"
          f"  ({payload / t_parse / 1e6:.1f} MB/s)")

    # ---- error classification ------------------------------------------ #
    errors = [line for line in lines if "ERROR" in line or "CRITICAL" in line
              or "refused" in line or "deadlock" in line or "OOM" in line]
    print(f"{'':44} {len(errors):,} lines contain error signals")

    def classify_errors() -> None:
        for line in errors:
            parse_entry(line, "app.log", 0)

    t_err = timeit("parse (error-heavy lines)", classify_errors)
    print(f"{'':44} {len(errors) / t_err:9,.0f} lines/s")

    # ---- stats ---------------------------------------------------------- #
    stats = LogStats()

    def fold_stats() -> None:
        for entry in entries:
            stats.record(entry)

    t_stats = timeit("stats fold (per line)", fold_stats)
    print(f"{'':44} {len(entries) / t_stats:9,.0f} lines/s")
    stats.reset()

    # ---- rates ---------------------------------------------------------- #
    def rates() -> None:
        for _ in range(1000):
            stats.rates()

    t_rates = timeit("1000 rate queries", rates)
    print(f"{'':44} {t_rates * 1e6 / 1000:9.1f} us/query")

    # ---- filter --------------------------------------------------------- #
    engine = FilterEngine(build_spec(min_level="ERROR", include="redis,db"))

    def apply_filter() -> None:
        engine.apply(entries)

    t_filter = timeit("filter (level + 2 substrings)", apply_filter)
    print(f"{'':44} {len(entries) / t_filter:9,.0f} entries/s")

    # ---- snapshot -------------------------------------------------------- #
    def snapshot() -> None:
        stats.snapshot()

    timeit("stats snapshot (UI tick)", snapshot)

    # ---- export ----------------------------------------------------------- #
    out = Path("/tmp/smartlog_bench")
    exporter = Exporter()
    subset = entries[:50_000]
    for fmt in ("md", "json", "txt", "html"):
        target = out / f"bench.{fmt}"
        timeit(f"export {fmt} ({len(subset):,} entries)",
               lambda f=fmt, t=target: exporter.write(subset, f, t))
        size = target.stat().st_size
        print(f"{'':44} {size / 1024:9,.0f} KiB output")

    print("-" * 62)
    peak = stats.snapshot().peak_lines_per_sec
    print(f"top signatures: {stats.snapshot().top_fingerprints[:5]}")
    print(f"peak per-second rate: {peak:.0f}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())