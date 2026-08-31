<div align="center">

# 🔍 SmartLog TUI
### Smart Log Analyzer & AI Diagnostics Tool

*A modern, asynchronous Terminal User Interface (TUI) application for real-time log monitoring, anomaly spike detection, and AI-powered root-cause diagnostics.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/UI-Textual-00C7B7?style=for-the-badge&logoColor=white)](https://textual.textualize.io/)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey?style=for-the-badge)](#)

</div>

---

## 💡 Why SmartLog TUI?

Tired of endless `tail -f` and messy `grep` commands? **SmartLog TUI** transforms your server log investigation into an intelligent, color-coded, and interactive dashboard right inside your terminal.

- ⚡ **Zero-Latency Async Streaming:** Reads multi-file log streams without blocking using `aiofiles` and ring buffers.
- 🧠 **Cascading AI Diagnostics:** Diagnoses errors with root-cause analysis and executable fix commands.
  - 🌐 *Cloud:* OpenAI (`gpt-4o-mini`, `gpt-4o`)
  - 💻 *Local:* Ollama / llama-server (`llama3`, `mistral`)
  - 📴 *Offline:* Built-in rule-based Knowledge Base
- 🚨 **Real-time Anomaly Detection:** Sliding-window error rate monitoring with instant burst/spike warnings.
- 📤 **Export & Share:** Export views to Markdown, JSON, HTML, or copy directly to system clipboard.
- 🔒 **License & Feature Gating:** Integrated with Lemon Squeezy for Free / Pro tier management.

---

## 📸 Overview

```text
┌────────────────────────────────── SmartLog TUI ───────────────────────────────────┐
│ [2026-08-30 20:00:01] [INFO   ] (syslog) Application booted on port 8080          │
│ [2026-08-30 20:00:04] [ERROR  ] (syslog) 500 Internal Server Error: Deadlock      │
│ [2026-08-30 20:00:07] [WARN   ] (nginx ) Slow query detected: 3200ms              │
├─────────────────────────────────┬─────────────────────────────────────────────────┤
│ === STATISTICS ===              │ === AI DIAGNOSIS ===                            │
│ Total Lines: 12,450             │ Summary: Database Deadlock Error                │
│ Throughput: 42.5 lines/sec      │ Confidence: VERY HIGH                           │
│ Total Errors: 14 (Rate: 2.0/min)│ Root Cause: Concurrent transaction conflicts.   │
│                                 │ Fix: $ systemctl status postgresql              │
└─────────────────────────────────┴─────────────────────────────────────────────────┘
```

---

## ⚡ Quick Start

### 1. Requirements
* Python **3.10+**
* Linux, macOS, or Windows

### 2. Installation

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/smartlog-tui.git
cd smartlog-tui

# Install dependencies
pip install -r requirements.txt
```

### 3. Running

```bash
# 1. Run with realistic simulated demo logs (Great for testing!)
python main.py --demo

# 2. Monitor default system logs (/var/log/*)
python main.py

# 3. Monitor specific log files
python main.py /var/log/nginx/error.log app.log

# 4. Non-interactive export to file or clipboard
python main.py --export md /var/log/syslog
python main.py --export clipboard app.log
```

---

## ⌨️ Keybindings

| Key | Action | Description |
| :--- | :--- | :--- |
| <kbd>a</kbd> | **AI Diagnose** | Run root-cause analysis on the latest/selected error |
| <kbd>f</kbd> | **Filter** | Set minimum log level (DEBUG -> CRITICAL) and keyword filters |
| <kbd>e</kbd> | **Export** | Open export dialog (Markdown, JSON, Plain Text, Clipboard) |
| <kbd>p</kbd> | **Pause/Resume** | Pause real-time stream tailing |
| <kbd>c</kbd> | **Clear** | Clear current visual log buffer |
| <kbd>r</kbd> | **Reset Stats** | Reset throughput and anomaly rate counters |
| <kbd>l</kbd> | **License** | Open license activation modal |
| <kbd>q</kbd> | **Quit** | Gracefully stop background tail workers and exit |

---

## 🤖 AI Diagnostics Configuration

SmartLog cascades through available diagnostic backends automatically:

1. **OpenAI (Cloud):** Set your API key in the environment:
   ```bash
   export OPENAI_API_KEY="sk-..."
   export OPENAI_MODEL="gpt-4o-mini"  # Optional (default: gpt-4o-mini)
   ```
2. **Ollama / llama-server (Local):** If no OpenAI key is set, it automatically connects to `http://localhost:11434` or `http://localhost:8080`.
3. **Offline Knowledge Base:** If no LLM endpoint is available, SmartLog falls back to its built-in rule-based expert system.

---

## 📁 Project Structure

```text
smartlog-tui/
├── main.py              # CLI entry point, argument parsing & demo generator
├── ui.py                # Textual TUI interface & modal dialogs
├── log_reader.py        # Asynchronous multi-file reader & ring buffer
├── parser.py            # Regex/JSON log parser & anomaly rate calculations
├── ai_diagnostics.py    # Cascading AI diagnostic engine & fallback knowledge base
├── exporter.py          # Multi-format renderer (MD/JSON/TXT) & clipboard integration
├── utils.py             # HTML report generator, percentile & sparkline helpers
├── license.py           # Lemon Squeezy license manager & feature gating
├── requirements.txt     # Python dependencies
└── README.md            # Documentation
```

---

## 📄 License

Distributed under the **MIT License**. See `LICENSE` for more information.

<div align="center">
  <sub>Built with ❤️ using <b>Python</b> and <b>Textual</b>.</sub>
</div>
