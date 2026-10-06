<div align="center">

# 🔍 SmartLog

### Fast terminal log analyser with cascading root-cause diagnostics

*Real-time multi-file log monitoring, anomaly spike detection, and AI-assisted
root-cause analysis — entirely in your terminal.*

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Textual](https://img.shields.io/badge/UI-Textual-00C7B7?style=flat-square&logoColor=white)](https://textual.textualize.io/)
[![Tests](https://img.shields.io/badge/tests-277%20passing-success?style=flat-square)](tests/)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

</div>

---

## 💡 Neden SmartLog?

`tail -f | grep` cevap vermiyor: hata *neden* oluştu? Bu proje, log akışını
izler, hatayı **tek seferde sınıflandırır**, anomaliyi **gerçek zamanlı** tespit
eder ve kök nedeni **çalıştırılabilir komutlarla** birlikte verir.

- ⚡ **Yüksek verim:** satır başına **tek** parse, O(1) istatistik, toplu (batch) render.
- 🧠 **Kademeli teşhis:** OpenAI / uyumlu LLM sunucusu → yerel Ollama/llama.cpp → **her zaman çalışan** kural tabanı.
- 🚨 **Anomali tespiti:** hata oranı, hareketli taban çizgisine göre oranlanır (sabit eşik değil).
- 📤 **Dışa aktarım:** Markdown, JSON, metin ve **tek dosyalık aranabilir HTML** raporu.
- 🧪 **277 test**, tamamen tip anotasyonlu, `mypy strict` uyumlu.

---

## ⚡ Hızlı Başlangıç

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1) Gerçekçi canlı demo akışı (en iyi ilk deneyim)
smartlog watch --demo

# 2) Kendi loglarını canlı izle
smartlog watch /var/log/syslog /var/log/nginx/error.log

# 3) Sadece hatalar, en az ERROR seviyesinde
smartlog watch app.log --errors-only -m ERROR

# 4) Bir kereye bakıp çık (TUI yok)
smartlog scan app.log --errors-only --top 15

# 5) Aranabilir HTML raporu üret
smartlog export app.log -f html -o incident.html

# 6) Ortam/provider kontrolü
smartlog doctor
```

`smartlog app.log` yazarsanız `watch` alt komutu varsayılır. `watch` verilmezse
**dizin** verilebilir; `.log/.txt/.out/.err` dosyaları özyinelemeli bulunur.

### Ortak seçenekler

| Seçenek | Açıklama |
| :--- | :--- |
| `-m, --min-level` | `TRACE`…`FATAL` arası minimum seviye |
| `-g, --grep` | virgülle ayrılmış alt diziler (hepsi eşleşmeli) |
| `-v, --exclude` | virgülle ayrılmış, çıkarılacak alt diziler |
| `-s, --source` | kaynak filtresi (tekrar edilebilir) |
| `--errors-only` | yalnızca hata ve üzeri |
| `-n, --tail N` | yalnızca son N satır (büyük dosyalarda hızlı) |
| `--encoding` | metin kodlaması (varsayılan `utf-8`) |

### Karma kodlama

Latin-1 / Windows-1252 / Shift-JIS kodlanmış loglar:

```bash
smartlog scan app.log --encoding latin-1
smartlog watch app.log --encoding cp1252
```

Çözülemeyen baytlar hata vermez, `errors="replace"` ile değiştirilir.

---

## ⌨️ Klavye Kısayolları

| Tuş | İşlev | Açıklama |
| :--- | :--- | :--- |
| <kbd>a</kbd> | **Diagnose** | Görünür alandaki son hatayı kök neden analiziyle incele |
| <kbd>f</kbd> | **Filter** | Seviye / kaynak / metin / regex filtreleme + vurgulama |
| <kbd>/</kbd> | **Search** | Filtre iletişim kutusunu vurgulama alanına odakla |
| <kbd>e</kbd> | **Export** | Markdown / JSON / metin / HTML olarak kaydet veya kopyala |
| <kbd>p</kbd> | **Pause** | Görünümü dondur (okuma ve istatistikler devam eder) |
| <kbd>c</kbd> | **Clear** | Görünümü ve halka tamponu temizle |
| <kbd>r</kbd> | **Reset stats** | Sayaçları ve anomali oranını sıfırla |
| <kbd>t</ | **Follow** | Otomatik kaydırmayı aç/kapat |
| <kbd>d</kbd> | **Debug view** | Mesaj yerine ham satırı göster |
| <kbd>?</kbd> | **Help** | Kısayolları hatırlat |
| <kbd>q</kbd> | **Quit** | Arka plan işçilerini düzgünce kapat |

Sağ panelde üsek sekme: **diagnose**, **signatures** (en sık hatalar), **files**
(dosya durumu / dönüşüm takibi).

---

## 🧠 Teşhis Motoru

`smartlog` teşhisi şu sırayla dener ve **her zaman** bir sonuç üretir:

| Sıra | Sağlayıcı | Nasıl yapılandırılır |
| :--- | :--- | :--- |
| 1 | OpenAI | `OPENAI_API_KEY` (+ isteğe bağlı `OPENAI_MODEL`) |
| 2 | Herhangi bir OpenAI-uyumlu sunucu | `SMARTLOG_LLM_URL`, `SMARTLOG_LLM_MODEL`, `SMARTLOG_LLM_API_KEY` |
| 3 | Ollama / llama.cpp / LM Studio | Otomatik: `localhost:11434`, `:8080`, `:1234` |
| 4 | **Çevrimdışı kural tabanı | Her zaman hazır, ağ gerekmez |

```bash
# Bulut
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4o-mini"

# Kendi sunucunuz (vLLM, TGI, OpenRouter, LM Studio...)
export SMARTLOG_LLM_URL="http://gpu-node:8000"
export SMARTLOG_LLM_MODEL="qwen2.5-32b-instruct"
export SMARTLOG_LLM_API_KEY="..."      # isteğe bağlı

# Tamamen çevrimdışı
smartlog watch app.log --offline-diag
export SMARTLOG_NO_LOCAL=1            # localhost taramasını kapat
```

**Önemli davranış:** tüm ağ çağrıları `asyncio.to_thread` ile ayrı thread'te
çalışır. Yani teşhis sırasında arayüz **donmaz** — bu, eski sürümdeki en
rahatsız edici davranışın düzeltilmiş halidir.

Çevrimdışı kural tabanı 16 senaryoyu kapsar (OOM, disk dolu, deadlock, bağlantı
reddi, timeout, izin, kimlik doğrulama, TLS, HTTP 4xx/5xx, circuit breaker,
traceback, panic, yavaş sorgu, dosya yok, rate limit, DNS, restart döngüsü,
veri bozulması) ve her biri **çalıştırılabilir komutlar** içerir.

---

## 📊 Anomali Tespiti Nasıl Çalışır?

Sabit bir "dakikada 10 hata" eşiği işe yaramaz: sessiz bir sistemde 10 hata
alarm, yoğun bir sistemde 10 hama yalnızlıktır. SmartLog bunun yerine:

1. Hataları **saniye çözünürlüğünde kovalara** (buckets) yazar — ekleme O(1).
2. Son 1 dakikanın hata oranını, **yavaş hareket eden EWMA taban çizgisiyle**
   karşılaştırır.
3. Hem **oran** (varsayılan 3x) hem de **mutlak hacim** eşiği (dakikada ≥5
   hata) sağlanırsa alarm üretir — böylece düşük hacimde yanlış pozitif olmaz.

```
 rate  128/s   errors  3,412  27/min   view  4,120 shown / 8 filtered
   ANOMALY: error rate 4.2x baseline
```

---

## 📁 Proje Yapısı

```text
smartlog/
├── models.py       Level (IntEnum) + LogEntry (slots)
├── parser.py       tek geçişli çok biçimli ayrıştırıcı + hata parmak izleri
├── stats.py        kovalı kayan pencere, EWMA taban çizgisi, sparkline
├── filters.py      filtre motoru + v1 `set_*` uyum şakası + vurgulama
├── reader.py       dosya başına thread, toplu okuma, rotasyon takibi
├── knowledge.py    16 senaryoluk çevrimdışı teşhis kuralı
├── diagnostics.py  async sağlayıcı kademesi + prompt birleştirme
├── exporters.py    streaming Markdown/JSON/metin + pano + encoding
├── html_report.py  tek dosyalık aranabilir HTML
├── utils.py        format_timestamp / percentile / human_readable_size ...
├── compat.py       v1 API uyum şakası (LogParser, LogStreamReader, ...)
├── app.py          Textual arayüz
├── demo.py         senaryo tabanlı gerçekçi log üretici
└── cli.py          watch / scan / export / doctor

tests/              340+ test (ayrıştırıcı, okuyucu, istatistik, teşhis,
                    filtre, dışa aktarım, CLI, utils, compat, headless UI)
benchmarks/         bench.py (ölçüm) + compare.py (v1 ile karşılaştırma)
.github/workflows/  ci.yml — Python 3.10–3.13 × Linux/macOS/Windows
```

### Kod tabanı sınırları

```
models  ←  parser  ←  {stats, filters, knowledge, reader, utils}
                        ↓
                  diagnostics, exporters
                        ↓
                    app  →  cli
```

`models.py` hiçbir şeyi import etmez. `compat.py` yalnızca v2'ye bağımlıdır
ve v2 onu import etmez — altıcılar taşındıktan sonra silinebilir.

---

## 🧪 Geliştirme

```bash
pytest                       # 340+ test
pytest tests/test_app.py     # sadece TUI (headless)
mypy smartlog                # strict tip kontrolü
ruff check smartlog tests
python benchmarks/bench.py --lines 200000
python benchmarks/compare.py --legacy <v1_klasörü>
```

CI (`.github/workflows/ci.yml`) Python 3.10–3.13 × Linux/macOS/Windows üzerinde
otomatik olarak bu üç kapıyı ve ölçüm betiğini çalıştırır.

### v1'den yükseltme

v2 genel API'yi yeniden adlandırdı. Kırılan yerler `smartlog/compat.py`
içinde bir **uyum şakası** olarak korundu:

```python
from smartlog.compat import (LogLevel, LogParser, LogStreamReader,
                             LogExporter, SystemClipboard,
                             ErrorKnowledgeBase, AIDiagnosticEngine)
```

`FilterEngine.set_level_filter()` gibi v1 `set_*` metotları ve
`utils.py` yardımcıları (`calculate_percentile`, `human_readable_size`,
`format_timestamp`, `truncate_string`, `escape_html`) doğrudan v2 API'sinde de
mevcut.

### Desteklenen log formatları

| Format | Örnek |
| :--- | :--- |
| ISO + seviye | `2026-08-30T20:00:01.123Z [ERROR] [api] payment failed` |
| ISO + seviye (boşluk) | `2026-08-30 20:00:01 WARN disk usage 91%` |
| JSON | `{"ts":"...","level":"error","msg":"pool exhausted","host":"web-01"}` |
| RFC 5424 seviye | `{"severity": 3, "message": "db down"}` |
| Syslog | `Aug 30 20:00:01 web01 sshd[812]: Failed password for root` |
| Nginx/Apache combined | `1.2.3.4 - - [...] "GET /x HTTP/1.1" 500 120 ...` |
| Köşeli seviye | `[2026-08-30 20:00:01] [INFO] service started` |
| Serbest biçim | `nginx: [error] 123#0: open() failed` |

---

## 📄 Lisans

MIT