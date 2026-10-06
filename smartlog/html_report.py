"""Self-contained, searchable HTML report writer.

Streamed entry-by-entry so a 200k-line export never materialises as one string.
All assets are inline: the report is a single file with no network dependency.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import TYPE_CHECKING, TextIO

from .models import Level, LogEntry

if TYPE_CHECKING:  # pragma: no cover
    from .exporters import ExportMeta
    from .stats import StatsSnapshot

__all__ = ["LEVEL_HEX", "write_html"]

LEVEL_HEX: dict[Level, str] = {
    Level.TRACE: "#64748b",
    Level.DEBUG: "#0ea5e9",
    Level.INFO: "#22c55e",
    Level.NOTICE: "#06b6d4",
    Level.WARN: "#f59e0b",
    Level.ERROR: "#ef4444",
    Level.CRITICAL: "#f43f5e",
    Level.FATAL: "#ffffff",
}

_CSS = """
:root{--bg:#0b1120;--panel:#111827;--edge:#1f2937;--fg:#e5e7eb;--muted:#94a3b8;--acc:#38bdf8}
*{box-sizing:border-box}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Consolas,monospace}
h1{margin:0 0 4px;font-size:22px}
h2{margin:24px 0 8px;font-size:15px;color:var(--acc);text-transform:uppercase;
 letter-spacing:.08em}
.sub{color:var(--muted);margin:0 0 16px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:8px;
 padding:10px 14px;min-width:130px}
.card .k{color:var(--muted);font-size:11px;text-transform:uppercase}
.card .v{font-size:19px;font-weight:700;color:var(--acc)}
table{border-collapse:collapse;width:100%;margin-bottom:18px}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--edge);font-size:13px}
th{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:11px}
.bar{height:6px;background:var(--edge);border-radius:3px;overflow:hidden;min-width:90px}
.bar>i{display:block;height:100%;background:var(--acc)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
input,select{background:var(--panel);border:1px solid #374151;color:var(--fg);
 padding:8px 12px;border-radius:6px;font-size:13px}
input{flex:1;min-width:220px}
.chk{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
#logs{border:1px solid var(--edge);border-radius:8px;overflow:hidden}
.row{display:flex;gap:10px;padding:4px 10px;border-left:3px solid transparent;
 font-family:Consolas,'Courier New',monospace;font-size:12.5px}
.row:hover{background:#0f172a}
.row.err{background:rgba(239,68,68,.09)}
.row.warn{background:rgba(245,158,11,.08)}
.row.hit{background:rgba(56,189,248,.16)}
.ts{color:var(--muted);flex:0 0 168px}
.lv{flex:0 0 74px;font-weight:700}
.src{color:#a78bfa;flex:0 0 130px;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap}
.ms{white-space:pre-wrap;word-break:break-word}
footer{color:var(--muted);font-size:12px;margin-top:20px;text-align:center}
"""

_JS = """
const rows=[...document.querySelectorAll('.row')],q=document.getElementById('q'),
 lvl=document.getElementById('lvl'),errs=document.getElementById('errs'),
 cnt=document.getElementById('cnt');
function apply(){
  const term=q.value.trim().toLowerCase(), min=lvl.value, onlyErr=errs.checked;
  let n=0;
  for(const r of rows){
    let ok=true;
    if(onlyErr&&!r.classList.contains('err'))ok=false;
    if(ok&&min!==''){const v=+r.dataset.lv;if(v<min)ok=false;}
    if(ok&&term&&!r.dataset.raw.includes(term))ok=false;
    r.style.display=ok?'':'none';
    if(ok)n++;
  }
  cnt.textContent=n.toLocaleString()+' / '+rows.length.toLocaleString();
}
q.addEventListener('input',apply);lvl.addEventListener('change',apply);
errs.addEventListener('change',apply);
document.addEventListener('keydown',e=>{
  if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();}
  if(e.key==='Escape'&&document.activeElement===q){q.value='';q.blur();apply();}
});
apply();
"""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def write_html(
    entries: Sequence[LogEntry],
    meta: ExportMeta,
    out: TextIO,
    stats: StatsSnapshot | None = None,
    *,
    level_colors: dict[Level, str] | None = None,
) -> None:
    """Write a standalone HTML report to ``out``."""
    colors = level_colors or LEVEL_HEX
    w = out.write
    esc = _esc

    w("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    w("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    w(f"<title>{esc(meta.application)} log export</title>")
    w(f"<style>{_CSS}</style></head><body>")

    # ---- header + stat cards ------------------------------------------- #
    w(f"<h1>{esc(meta.application)} &mdash; log export</h1>")
    w(f"<p class=\"sub\">generated {_esc(meta.generated_at)} &middot; "
      f"filters: {_esc(meta.filters)} &middot; "
      f"sources: {_esc(', '.join(meta.sources) or 'N/A')}</p>")

    cards = [
        ("entries", f"{meta.total:,}"),
        ("errors", f"{meta.total_errors:,}"),
        ("error rate", f"{meta.error_rate_per_min:.1f}/min"),
    ]
    if stats is not None:
        cards += [
            ("throughput", f"{stats.lines_per_sec:.1f}/s"),
            ("peak", f"{stats.peak_lines_per_sec:.0f}/s"),
            ("uptime", f"{stats.uptime_sec:.0f}s"),
        ]
        if stats.dropped_lines:
            cards.append(("dropped", f"{stats.dropped_lines:,}"))
        if stats.sparkline:
            cards.append(("rate 60s", stats.sparkline))
    w("<div class=\"cards\">")
    for key, value in cards:
        w(f"<div class=\"card\"><div class=\"k\">{esc(key)}</div>"
          f"<div class=\"v\">{esc(value)}</div></div>")
    w("</div>")

    # ---- level distribution -------------------------------------------- #
    if meta.level_counts:
        w("<h2>Levels</h2><table><tr><th>Level</th><th>Count</th>"
          "<th style=\"width:130px\">Share</th></tr>")
        peak = max(meta.level_counts.values()) or 1
        for level, count in meta.level_counts.items():
            w(f"<tr><td>{esc(level)}</td><td>{count:,}</td>"
              f"<td><div class=\"bar\"><i style=\"width:"
              f"{count / peak * 100:.1f}%\"></i></div></td></tr>")
        w("</table>")

    # ---- top signatures -------------------------------------------------- #
    if stats is not None and stats.top_fingerprints:
        w("<h2>Top error signatures</h2><table><tr><th>Signature</th>"
          "<th>Count</th></tr>")
        for name, count in stats.top_fingerprints:
            w(f"<tr><td>{esc(name)}</td><td>{count:,}</td></tr>")
        w("</table>")

    # ---- toolbar ---------------------------------------------------------- #
    w("<h2>Entries</h2><div class=\"toolbar\">")
    w("<input id=\"q\" type=\"search\" placeholder=\"Search (press / to focus)\">")
    w("<select id=\"lvl\"><option value=''>all levels</option>")
    for level in ("TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "ERROR",
                  "CRITICAL", "FATAL"):
        w(f"<option value=\"{Level[level].value}\">{level}</option>")
    w("</select>")
    w("<label class=\"chk\"><input type=\"checkbox\" id=\"errs\"> errors only</label>")
    w("<span class=\"chk\" id=\"cnt\"></span>")
    w("</div><div id=\"logs\">")

    # ---- rows (streamed) --------------------------------------------------- #
    for entry in entries:
        cls = ""
        if entry.level >= Level.ERROR:
            cls = "err"
        elif entry.level >= Level.WARN:
            cls = "warn"
        colour = colors.get(entry.level, "#e5e7eb")
        raw_lower = entry.raw.lower()
        # data-raw holds a compact, escaped-lowercase haystack for JS filtering.
        w(f"<div class=\"row {cls}\" data-lv=\"{int(entry.level)}\" "
          f"data-raw=\"{esc(raw_lower)}\">")
        w(f"<span class=\"ts\">{esc(entry.timestamp or '-')}</span>")
        w(f"<span class=\"lv\" style=\"color:{colour}\">{entry.level.label}</span>")
        if entry.source:
            w(f"<span class=\"src\" title=\"{esc(entry.source)}\">"
              f"{esc(entry.source)}</span>")
        w(f"<span class=\"ms\">{esc(entry.message or entry.raw)}</span></div>\n")

    w("</div>")
    w(f"<footer>{esc(meta.application)} v{esc(meta.version)} &middot; "
      f"{meta.total:,} entries &middot; window {_esc(meta.first_seen)} "
      f"&rarr; {_esc(meta.last_seen)}</footer>")
    w(f"<script>{_JS}</script></body></html>\n")