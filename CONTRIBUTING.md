# Katkıda Bulunma · Contributing

SmartLog'a katkıda bulunduğunuz için teşekkürler. Bu proje tamamen gönüllü
katkılarla sürdürülüyor.

---

## Hızlı başlangıç

```bash
git clone https://github.com/insanazor822/smartlog.git
cd smartlog

python -m venv .venv
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
pip install -e ".[dev]"
```

## Çalıştırmadan önce

Proje üç kapıdan geçmelidir. CI da tam olarak bunları çalıştırır:

```bash
ruff check smartlog tests benchmarks   # stil
mypy smartlog                          # tip
pytest                                 # testler
```

Üçü de yeşil olmadan PR açmayın.

---

## Test

```bash
pytest                          # tümü (~60 sn)
pytest tests/test_parser.py     # tek modül
pytest -k "rotation"            # filtre
pytest -k "not slow"            # hızlı döngü
```

Testler üç kategoriden oluşur:

| Tip | Nerede | Kural |
| :--- | :--- | :--- |
| Davranış testi | `tests/test_*.py` | Her yeni davranış için test |
| Performans guard | `test_parser`, `test_stats`, `test_filters` | Regresyonu yakalar (ör. "20k satır < 1 sn") |
| TUI duman testi | `tests/test_app.py` | Textual'ı `importorskip` eder; terminal yoksa atlanır |

### Test yazarken

- **Önce regresyon testini yazın**, sonra düzeltmeyi. Test, hatayı önce
  göstermelidir (kırmızı), düzeltmeden sonra yeşile dönmelidir.
- Yanlış pozitifleri de testleyin. `test_parser.py::TestFalsePositives`
  "512ms HTTP 5xx sanılmıyor" gibi durumları korur.
- **Zamanlayıcıya güvenmeyin.** `asyncio.sleep(0.4)` yerine bir koşul döngüsü
  yazın; aksi halde CI'da flaky olur.
- Rastgelelik kullanıyorsanız sabit `seed` verin.

---

## Kod standartları

- **Python 3.10** taban çizgi (bkz. `pyproject.toml`).
- Her yeni fonksiyonda tam tip anotasyonu; `mypy` strict modunda temiz olmalı.
- **Sürdürülebilirlik yorumları:** Neden yaptığınızı yazın. Özellikle performans
  kararlarını gerekçelendirin — "bu O(n) çünkü ..." gibi.
- Kod tabanı dili: **İngilizce** (docstring, yorum, commit). Arayüz metinleri
  de İngilizce.
- Yorumlarda satır sonu yorumları kullanmayın; gerekiyorsa üstlerine yazın.

### Mimî sınırlar

Bağımlılık yönü şudur, tersine bağımlılık kurmayın:

```
models  ←  parser  ←  {stats, filters, knowledge, reader}
                            ↓
                      diagnostics, exporters
                            ↓
                          app  →  cli
```

- `models.py` hiçbir şeyi import etmez.
- Yeni bir parser yazacaksanız `parser.py`'ye ekleyin; ikinci bir ayrıştırıcı
  modül açmayın. (Bu, v1'in en büyük yapısal hatasıydı.)
- `app.py` içinde `query_one` çağrılarını `_value()` yardımcısıyla sarmalayın.

---

## Commit mesajları

```
<tip>(<kapsam>): <kısa özet>

<gövde: ne değişti ve neden>

<altbilgi: BREAKING CHANGE / Closes #42>
```

Tipler: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`, `perf`.

Örnek:
```
perf(stats): replace O(n) per-line error-rate scan with second buckets

The old implementation summed the whole five-minute deque on every incoming
line, which cost 52s on a 100k-line file. Rates are now maintained with
per-second counters and only evaluated on the 1 Hz UI tick: 512x faster.
```

---

## Pull request

1. **Bir PR = bir konu.** Refactor ile davranış değişikliğini aynı PR'a koymayın.
2. Önce açıklama yazın: neyi, neden, nasıl doğruladığınızı.
3. `pytest` çıktısını PR açıklamasına ekleyin.
4. Ölçüm değiştirdiyseniz `benchmarks/bench.py` çıktısını paylaşın.
5. Dokümantasyon gerekiyorsa `README.md`'yi güncelleyin — doküman güncel olmazsa
   PR tamamlanmış sayılmaz.

---

## Uyumluluk (v1 → v2)

Bu proje v1'den yükseltilmiş bir koddur. Genel API kırıcı değişiklikler
yapıldı, ancak en sık kullanılan parçalar için **uyum şakası** korundu
(`FilterEngine.set_*`, `utils.py` yardımcıları, `read_log_file(encoding=)`).

Bu yüzden:

- **Public API'yi sessizce değiştirmeyin.** Değiştiriyorsanız eski ismi bir
  şaka olarak bırakın ve `tests/` içinde uyumluluk testi yazın.
- Yeni bir public fonksiyon eklerken `__init__.py` içindeki `__all__` listesini
  güncelleyin (`tests/test_integration.py` bunu doğruluyor).

---

## Güvenlik

Bir güvenlik açığı bulduğunuzu düşünüyorsanız lütfen **public issue açmayın**.
Önce `smartlog doctor` çıktısıyla birlikte ayrıntılı bir özet hazırlayın.

---

## İletişim

Sorular ve öneriler için GitHub Discussions veya issue kullanabilirsiniz.