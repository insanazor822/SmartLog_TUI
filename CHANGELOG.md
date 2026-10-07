# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [2.0.0] - 2026-10-07

First release of the rewritten terminal log analyser. The v1 prototype was a
flat script; v2 is a packaged, typed, tested project.

### Added

- **Cascading diagnosis.** Probes OpenAI-compatible servers, then local Ollama /
  llama.cpp / LM Studio, then falls back to an offline rule engine with **19
  scenarios**. Always returns a result, even with no network.
- **Anomaly detection** scored against a slow-moving EWMA baseline rather than a
  fixed threshold, with both a ratio (3x) and an absolute volume floor so quiet
  systems do not false-positive.
- **Export** to Markdown, JSON, plain text and a single-file searchable HTML
  report.
- **`smartlog doctor`** for environment and provider checks.
- **Encoded-log support.** Latin-1 / Windows-1252 / Shift-JIS via `--encoding`;
  undecodable bytes are replaced rather than raising.
- **v1 compatibility shim** in `smartlog.compat`, preserving the v1 class names
  and `set_*` filter methods so existing scripts keep working.

### Changed

- Single parse per line; `LogEntry` is the only representation. Statistics are
  O(1) and rendering is batched, replacing a per-line render and a double parse.
- Diagnosis moved off the event loop into an `asyncio` worker, so the UI no
  longer freezes while a provider is queried.
- One ring buffer is the only store; the visible list is derived by filtering.
- Keyboard-driven TUI with an error-focused view, top-signature list, pause,
  follow toggle, copy/export and a searchable filter dialog.
- README translated to English and corrected against the code.

### Fixed

- **Shutdown deadlock.** `action_quit` awaited its own cancelled tasks while
  `_tick` was inside synchronous Rich rendering, where Python cannot deliver a
  cancellation. The app could never exit; it surfaced as `WaitForScreenTimeout`
  on Python 3.10 and as a full hang on 3.11. Tasks are now cancelled and not
  awaited.
- **Double-counted statistics.** `_prime_view` recorded the ring buffer while
  `_pump` recorded the same entries from the batch queue, so every total was
  inflated whenever the reader outran the prime. `_pump` is now the only owner
  of the counts.
- **Rotation on Windows.** The reader opened files without `FILE_SHARE_DELETE`,
  so any external rename failed with `WinError 32` and the tailer could not
  follow a rotated file at all. Files are now opened through `CreateFileW` with
  delete-sharing, and renames retry briefly.
- **Distribution keeps the `smartlog-tui` name.** The bare `smartlog` name on PyPI
  belongs to an unrelated package, so it is not available. The importable
  package and the console command are still `smartlog`.

### Changed in packaging

- PyPI distribution: `smartlog-tui`. `pip install smartlog-tui`; `smartlog` as an
  import and as a command.
- Python 3.10–3.13, verified on Linux, macOS and Windows via CI.
- 386 tests, `mypy strict` clean, `py.typed` shipped.

[Unreleased]: https://github.com/insanazor822/SmartLog_TUI/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/insanazor822/SmartLog_TUI/releases/tag/v2.0.0