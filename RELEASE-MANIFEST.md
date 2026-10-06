# SmartLog - GitHub yükleme paketi

Bu klasör `scripts/prepare_release.py` tarafından üretildi ve yalnızca
sürüm kontrolüne girmesi gereken dosyaları içerir.

## Özet

- Dosya sayısı: **39**
- Toplam boyut: **354.8 KiB**
- Atlanan dosya: **29**
- Reddedilen gizli dosya: **0**
- Reddedilen büyük dosya: **0**

## Dahil edilen dosyalar

| Dosya | Boyut |
| :--- | ---: |
| `pyproject.toml` | 2,473 B |
| `README.md` | 9,686 B |
| `LICENSE` | 1,069 B |
| `CONTRIBUTING.md` | 4,668 B |
| `MANIFEST.in` | 611 B |
| `.gitignore` | 2,316 B |
| `.gitattributes` | 827 B |
| `smartlog/__init__.py` | 1,835 B |
| `smartlog/__main__.py` | 155 B |
| `smartlog/app.py` | 33,271 B |
| `smartlog/cli.py` | 13,986 B |
| `smartlog/compat.py` | 10,030 B |
| `smartlog/demo.py` | 11,920 B |
| `smartlog/diagnostics.py` | 21,617 B |
| `smartlog/exporters.py` | 17,005 B |
| `smartlog/filters.py` | 8,833 B |
| `smartlog/html_report.py` | 8,426 B |
| `smartlog/knowledge.py` | 27,917 B |
| `smartlog/models.py` | 5,231 B |
| `smartlog/parser.py` | 26,563 B |
| `smartlog/py.typed` | 0 B |
| `smartlog/reader.py` | 15,799 B |
| `smartlog/stats.py` | 11,969 B |
| `smartlog/utils.py` | 3,649 B |
| `tests/conftest.py` | 1,075 B |
| `tests/test_app.py` | 10,836 B |
| `tests/test_cli.py` | 9,441 B |
| `tests/test_demo.py` | 5,633 B |
| `tests/test_diagnostics.py` | 15,433 B |
| `tests/test_exporters.py` | 15,935 B |
| `tests/test_filters.py` | 9,931 B |
| `tests/test_integration.py` | 11,436 B |
| `tests/test_parser.py` | 7,419 B |
| `tests/test_reader.py` | 12,872 B |
| `tests/test_stats.py` | 5,727 B |
| `tests/test_utils.py` | 4,190 B |
| `benchmarks/bench.py` | 4,999 B |
| `benchmarks/compare.py` | 6,435 B |
| `.github/workflows/ci.yml` | 2,119 B |

## Atlanan dosyalar

| Dosya | Neden |
| :--- | :--- |
| `smartlog/__pycache__/__init__.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/__main__.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/app.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/cli.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/compat.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/demo.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/diagnostics.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/exporters.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/filters.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/html_report.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/knowledge.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/models.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/parser.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/reader.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/stats.cpython-314.pyc` | kara liste dizini |
| `smartlog/__pycache__/utils.cpython-314.pyc` | kara liste dizini |
| `tests/__pycache__/conftest.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_app.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_cli.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_demo.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_diagnostics.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_exporters.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_filters.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_integration.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_parser.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_reader.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_stats.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `tests/__pycache__/test_utils.cpython-314-pytest-9.1.1.pyc` | kara liste dizini |
| `benchmarks/__pycache__/compare.cpython-314.pyc` | kara liste dizini |

## Doğrulama

Bütünlük için `CHECKSUMS.sha256` dosyasını kullanın:

```bash
cd release && sha256sum -c CHECKSUMS.sha256
```

## Yükleme

```bash
cd release
git init -b main
git add .
git commit -m 'feat: SmartLog v2.0.0 - log analyzer with cascading diagnostics'
git remote add origin https://github.com/<kullanici>/smartlog.git
git push -u origin main
```
