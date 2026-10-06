#!/usr/bin/env python3
"""Side-by-side benchmark: legacy (v1) pipeline vs the rewritten pipeline.

Requires the original ``smartlog_no_license`` directory next to this repo, or a
path passed with ``--legacy``.

    python benchmarks/compare.py --legacy ../smartlog_no_license
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW_ROOT = HERE.parent
sys.path.insert(0, str(NEW_ROOT))

TEMPLATES = [
    "2026-08-30T20:00:01.123Z [INFO] [api] GET /api/v1/orders 200 12ms",
    "2026-08-30T20:00:02.456Z [DEBUG] [db] SELECT * FROM orders WHERE id = 1",
    "2026-08-30T20:00:03.789Z [WARN] [db] slow query detected: 3200ms in get_user()",
    "2026-08-30T20:00:04.001Z [ERROR] [db] deadlock detected while waiting for lock",
    "2026-08-30T20:00:05.111Z [CRITICAL] Out of memory: OOMKilled process 1",
    "2026-08-30T20:00:06.222Z [ERROR] [cache] Connection refused: redis://127.0.0.1:6379",
    "2026-08-30T20:00:07.333Z [INFO] [worker] job 1234 completed in 44ms",
    '{"ts":"2026-08-30T20:00:08.444Z","level":"error","msg":"pool exhausted","host":"web-01"}',
    "Aug 30 20:00:09 web01 sshd[812]: Failed password for root from 203.0.113.7",
    '203.0.113.9 - - [30/Aug/2026:20:00:10 +0000] "GET /api/v1/x HTTP/1.1" 500 120 "-" "curl/8"',
]


def make_lines(count: int) -> list[str]:
    return [TEMPLATES[i % len(TEMPLATES)] for i in range(count)]


NEW_SCRIPT = r'''
import json, sys, time
sys.path.insert(0, {root!r})
from smartlog import parse_entry, LogStats, FilterEngine, build_spec

lines = json.load(open({lines!r}))
out = {{}}

start = time.perf_counter()
entries = [parse_entry(l, "app.log", i) for i, l in enumerate(lines)]
out["parse"] = time.perf_counter() - start

stats = LogStats()
start = time.perf_counter()
for e in entries:
    stats.record(e)
stats.snapshot()
out["stats"] = time.perf_counter() - start

engine = FilterEngine(build_spec(min_level="ERROR", include="redis,db"))
start = time.perf_counter()
for _ in range(10):
    engine.apply(entries)
out["filter10"] = time.perf_counter() - start

out["errors"] = sum(1 for e in entries if e.is_error)
print(json.dumps(out))
'''

LEGACY_SCRIPT = r'''
import json, sys, time
sys.path.insert(0, {root!r})
from log_reader import LogStreamReader
from parser import LogParser, FilterEngine
from parser import LogLevel

lines = json.load(open({lines!r}))
out = {{}}

reader = LogStreamReader(["app.log"], buffer_size=10)
start = time.perf_counter()
entries = [reader._parse_line(l, "app.log", i) for i, l in enumerate(lines)]
out["parse"] = time.perf_counter() - start

pl = LogParser()
stats = pl.stats
anomaly = pl.anomaly_stats
start = time.perf_counter()
for i, raw in enumerate(lines):
    pl.update_statistics(raw, "app.log")
pl.refresh_rates()
out["stats"] = time.perf_counter() - start

engine = FilterEngine(pl)
engine.set_level_filter(LogLevel.ERROR)
engine.set_text_filter("redis")
start = time.perf_counter()
for _ in range(10):
    [engine.matches(e) for e in entries]
out["filter10"] = time.perf_counter() - start

out["errors"] = anomaly.total_errors
print(json.dumps(out))
'''


def run(script: str, root: Path, lines_file: Path) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(script.format(root=str(root), lines=str(lines_file)))
        script_path = handle.name
    try:
        proc = subprocess.run([sys.executable, script_path], capture_output=True,
                              text=True, cwd=tempfile.gettempdir())
        if proc.returncode != 0:
            return {"error": proc.stderr.strip().splitlines()[-1:]}
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        Path(script_path).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", type=Path, default=None,
                        help="path to the original smartlog_no_license directory")
    parser.add_argument("--lines", type=int, default=100_000)
    args = parser.parse_args()

    if args.legacy is None:
        print("--legacy <dir> is required (the v1 source tree)")
        return 2
    if not (args.legacy / "parser.py").exists():
        print(f"no legacy parser.py under {args.legacy}")
        return 2

    lines = make_lines(args.lines)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(lines, handle)
        lines_file = Path(handle.name)

    print(f"\n{args.lines:,} mixed-format lines\n" + "=" * 66)
    try:
        legacy = run(LEGACY_SCRIPT, args.legacy.resolve(), lines_file)
        new = run(NEW_SCRIPT, NEW_ROOT, lines_file)
    finally:
        lines_file.unlink(missing_ok=True)

    if "error" in legacy or "error" in new:
        print(f"legacy: {legacy}")
        print(f"new   : {new}")
        return 1

    print(f"{'stage':<36}{'legacy':>12}{'new':>12}{'speedup':>10}")
    print("-" * 70)
    for key, label in (
        ("parse", "structure parse only"),
        ("stats", "error detection + stats"),
        ("filter10", "filter x10"),
    ):
        old, new_t = legacy[key], new[key]
        ratio = old / new_t if new_t else float("inf")
        print(f"{label:<36}{old * 1000:>10.1f}ms{new_t * 1000:>10.1f}ms"
              f"{ratio:>9.1f}x")

    print("-" * 70)
    # The honest headline number. v1 did error detection inside its stats pass,
    # so parse+stats is the comparable unit: the new pipeline additionally
    # produces fingerprints, severities and epoch timestamps.
    old_total = legacy["parse"] + legacy["stats"]
    new_total = new["parse"] + new["stats"]
    print(f"{'parse + error detect (total)':<36}{old_total * 1000:>10.1f}ms"
          f"{new_total * 1000:>10.1f}ms{old_total / new_total:>9.1f}x")
    print(f"{'end-to-end throughput':<36}{args.lines / old_total:>10,.0f}/s"
          f"{args.lines / new_total:>11,.0f}/s")
    print("-" * 70)
    print(f"{'errors found (legacy)':<36}{legacy['errors']:>12}")
    print(f"{'errors found (new)':<36}{new['errors']:>12}")
    print("\nNote: v1 only counted an error when the level token or one of its\n"
          "loose regexes fired; the new pipeline also classifies by signature, so\n"
          "a higher count here is expected (and correct).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())