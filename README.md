<div align="center">

# 🔍 SmartLog

### Fast terminal log analyser with cascading root-cause diagnostics

*Real-time multi-file log monitoring, anomaly spike detection, and AI-assisted
root-cause analysis — entirely in your terminal.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/UI-Textual-00C7B7?style=flat-square&logo=whale&logoColor=white)](https://textual.textualize.io/)
[![Tests](https://img.shields.io/badge/tests-277%20passing-success?style=flat-square)](tests/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

## 💡 Why SmartLog?

`tail -f | grep` doesn't answer the question: *why* did the error happen? SmartLog
follows the log stream, classifies each error **exactly once**, detects anomalies
**in real time**, and returns the root cause together with **runnable commands**.

- ⚡ **High throughput:** a **single** parse per line, O(1) statistics, batched rendering.
- 🧠 **Cascading diagnosis:** OpenAI-compatible server → local Ollama/llama.cpp → **always-available** offline rule engine.
- 🚨 **Anomaly detection:** error rate is scored against a moving baseline, not a fixed threshold.
- 📤 **Export:** Markdown, JSON, plain text, and a **single-file searchable HTML** report.
- 🧪 **277 tests**, fully type-annotated, `mypy strict` clean.

---

## ⚡ Quick Start

```bash
# Install from PyPI
pip install smartlog-tui

# Or from a source checkout
git clone https://github.com/insanazor822/SmartLog_TUI.git
cd SmartLog_TUI
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1) Realistic live demo stream (the best first impression)
smartlog watch --demo

# 2) Watch your own logs live
smartlog watch /var/log/syslog /var/log/nginx/error.log

# 3) Errors only, at ERROR level or above
smartlog watch app.log --errors-only -m ERROR

# 4) One-shot analysis, no TUI
smartlog scan app.log --errors-only --top 15

# 5) Produce a searchable HTML report
smartlog export app.log -f html -o incident.html

# 6) Check providers and environment
smartlog doctor
```

Writing `smartlog app.log` implies the `watch` subcommand. When no file is given to
`watch`, a **directory** is accepted instead and `.log`/`.txt`/`.out`/`.err` files
are discovered recursively.

> **Note on naming.** The PyPI distribution is **`smartlog-tui`**. The bare
> `smartlog` name on PyPI belongs to an unrelated package, so it is not
> available; the importable Python package and the console command are both
> still called `smartlog`: `from smartlog import …` and `smartlog watch …`.

### Shared options

| Option | Description |
| :--- | :--- |
| `-m, --min-level` | Minimum level between `TRACE`…`FATAL` |
| `-g, --grep` | Comma-separated substrings (all must match) |
| `-v, --exclude` | Comma-separated substrings to exclude |
| `-s, --source` | Source filter (repeatable) |
| `--errors-only` | Errors and above only |
| `-n, --tail N` | Last N lines only (fast on large files) |
| `--encoding` | Text encoding (default `utf-8`) |

### Additional subcommand options

| Option | Applies to | Description |
| :--- | :--- | :--- |
| `--buffer N` | `watch` | Ring-buffer capacity (default 50,000 lines) |
| `--from-start` | `watch` | Replay existing content instead of tailing only new lines |
| `--no-anomaly` | `watch` | Disable anomaly detection |
| `--offline-diag` | `watch` | Skip all network probes, use the rule engine only |
| `--top N` | `scan` | Number of top signatures to report |
| `-f, --format` | `export` | Output format: `md`/`markdown`, `json`, `txt`/`text`, `html` |
| `-o, --output` | `export` | Output path (defaults to a generated filename) |
| `--all` | `export` | Export every entry instead of errors only |
| `--clipboard` | `export` | Copy the result to the system clipboard |

### Mixed encodings

Logs encoded in Latin-1 / Windows-1252 / Shift-JIS:

```bash
smartlog scan app.log --encoding latin-1
smartlog watch app.log --encoding cp1252
```

Undecodable bytes never raise: they are replaced via `errors="replace"`.

---

## ⌨️ Keyboard Shortcuts

| Key | Action | Description |
| :--- | :--- | :--- |
| <kbd>a</kbd> | **Diagnose** | Run root-cause analysis on the last visible error |
| <kbd>f</kbd> | **Filter** | Filter by level / source / text / regex, with highlighting |
| <kbd>/</kbd> | **Search** | Focus the highlight field inside the filter dialog |
| <kbd>e</kbd> | **Export** | Save or copy as Markdown / JSON / text / HTML |
| <kbd>p</kbd> | **Pause** | Freeze the view (reading and statistics keep running) |
| <kbd>c</kbd> | **Clear** | Clear the view and the ring buffer |
| <kbd>r</kbd> | **Reset stats** | Reset counters and the anomaly ratio |
| <kbd>t</kbd> | **Follow** | Toggle auto-scroll |
| <kbd>d</kbd> | **Debug view** | Show the raw line instead of the parsed message |
| <kbd>?</kbd> | **Help** | Recall the shortcut list |
| <kbd>q</kbd> | **Quit** | Shut the background workers down cleanly |

The right panel has three tabs: **diagnose**, **signatures** (most frequent
errors), and **files** (per-file state and rotation tracking).

---

## 🧠 Diagnosis Engine

`smartlog` tries providers in this order and **always** produces a result:

| Order | Provider | How to configure |
| :--- | :--- | :--- |
| 1 | OpenAI | `OPENAI_API_KEY` (+ optional `OPENAI_MODEL`) |
| 2 | Any OpenAI-compatible server | `SMARTLOG_LLM_URL`, `SMARTLOG_LLM_MODEL`, `SMARTLOG_LLM_API_KEY` |
| 3 | Ollama / llama.cpp / LM Studio | Automatic: `localhost:11434`, `:8080`, `:1234` |
| 4 | **Offline rule engine** | Always available, requires no network |

```bash
# Cloud
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4o-mini"

# Your own server (vLLM, TGI, OpenRouter, LM Studio, …)
export SMARTLOG_LLM_URL="http://gpu-node:8000"
export SMARTLOG_LLM_MODEL="qwen2.5-32b-instruct"
export SMARTLOG_LLM_API_KEY="..."      # optional

# Fully offline
smartlog watch app.log --offline-diag
export SMARTLOG_NO_LOCAL=1            # skip localhost probing entirely
```

**Key behaviour:** every network call runs in a separate thread via
`asyncio.to_thread`, so the UI **never freezes** during diagnosis. This is the
corrected version of the most annoying behaviour in the previous release.

Endpoint discovery is cached and probed once, and all endpoints are queried
concurrently under an overall deadline — so a machine without an LLM server
does not pay a timeout penalty on every keystroke.

The offline rule engine covers **19 scenarios** (OOM, disk full, deadlock,
connection refused, timeout, permission, authentication, TLS, HTTP 4xx/5xx,
circuit breaker, traceback, panic, slow query, file not found, rate limit, DNS,
restart loop, data corruption), each with **runnable commands**.

---

## 📊 How Anomaly Detection Works

A fixed "10 errors per minute" threshold is useless: in a quiet system 10 errors
are an incident, in a busy system they are noise. SmartLog instead:

1. Writes errors into **second-resolution buckets** — insertion is O(1).
2. Compares the last minute's error rate against a **slow-moving EWMA baseline**.
3. Raises an alert only when **both** a ratio (default 3x) **and** an absolute
   volume floor (≥5 errors/min) are met — so low traffic never false-positives.

```
  rate  128/s   errors  3,412  27/min   view  4,120 shown / 8 filtered
   ANOMALY: error rate 4.2x baseline
```

---

## 📁 Project Structure

```text
smartlog/
├── models.py       Level (IntEnum) + LogEntry (slots)
├── parser.py       single-pass multi-format parser + error fingerprints
├── stats.py        bucketed sliding window, EWMA baseline, sparkline
├── filters.py      filter engine + v1 `set_*` compat shim + highlighting
├── reader.py       thread per file, batched reads, rotation tracking
├── knowledge.py    19-scenario offline diagnosis rule base
├── diagnostics.py  async provider cascade + prompt assembly
├── exporters.py    streaming Markdown/JSON/text + board + encoding
├── html_report.py  single-file searchable HTML
├── utils.py        format_timestamp / percentile / human_readable_size / …
├── compat.py       v1 API compat shim (LogParser, LogStreamReader, …)
├── app.py          Textual interface
├── demo.py         scenario-based realistic log generator
└── cli.py          watch / scan / export / doctor

tests/              277 tests (parser, reader, stats, diagnostics,
                    filters, exporters, CLI, utils, compat, headless UI)
benchmarks/         bench.py (measurement) + compare.py (v1 comparison)
.github/workflows/  ci.yml — Python 3.10–3.13 × Linux/macOS/Windows
```

### Codebase boundaries

```
models  ←  parser  ←  {stats, filters, knowledge, reader, utils}
                         ↓
                   diagnostics, exporters
                         ↓
                      app  →  cli
```

`models.py` imports nothing. `compat.py` depends only on v2, and v2 never imports
it — once the v1 shims are retired, it can be deleted outright.

---

## 🧪 Development

```bash
pytest                       # 277 tests
pytest tests/test_app.py     # TUI only (headless)
mypy smartlog                # strict type check
ruff check smartlog tests benchmarks
python benchmarks/bench.py --lines 200000
python benchmarks/compare.py --legacy <v1_directory>
```

CI (`.github/workflows/ci.yml`) runs these three gates plus the benchmark script
across Python 3.10–3.13 on Linux, macOS, and Windows.

### Upgrading from v1

v2 renamed the public API. The breakages are preserved in `smartlog/compat.py`
as a **compat shim**:

```python
from smartlog.compat import (LogLevel, LogParser, LogStreamReader,
                             LogExporter, SystemClipboard,
                             ErrorKnowledgeBase, AIDiagnosticEngine)
```

v1 `set_*` methods such as `FilterEngine.set_level_filter()`, and the `utils.py`
helpers (`calculate_percentile`, `human_readable_size`, `format_timestamp`,
`truncate_string`, `escape_html`), are also available directly on the v2 API.

### Supported log formats

| Format | Example |
| :--- | :--- |
| ISO + level | `2026-08-30T20:00:01.123Z [ERROR] [api] payment failed` |
| ISO + level (space) | `2026-08-30 20:00:01 WARN disk usage 91%` |
| JSON | `{"ts":"...","level":"error","msg":"pool exhausted","host":"web-01"}` |
| RFC 5424 severity | `{"severity": 3, "message": "db down"}` |
| Syslog | `Aug 30 20:00:01 web01 sshd[812]: Failed password for root` |
| Nginx/Apache combined | `1.2.3.4 - - [...] "GET /x HTTP/1.1" 500 120 ...` |
| Bracketed level | `[2026-08-30 20:00:01] [INFO] service started` |
| Free-form | `nginx: [error] 123#0: open() failed` |

---

## 📄 License

MIT