"""Filter engine tests."""

from __future__ import annotations

import pytest

from smartlog.filters import FilterEngine, FilterSpec, build_spec, highlight_terms
from smartlog.models import Level, LogEntry
from smartlog.parser import LineParser


@pytest.fixture(scope="module")
def parser() -> LineParser:
    return LineParser()


def make(level: Level = Level.INFO, *, source: str = "app.log",
         raw: str = "plain message") -> LogEntry:
    return LogEntry(raw=raw, source=source, level=level, message=raw)


class TestLevelFilter:
    def test_min_level_passes_higher(self):
        engine = FilterEngine(build_spec(min_level="WARN"))
        assert engine.matches(make(Level.ERROR))
        assert engine.matches(make(Level.CRITICAL))
        assert not engine.matches(make(Level.INFO))

    def test_min_level_accepts_int_enum(self):
        engine = FilterEngine(build_spec(min_level=Level.ERROR))
        assert engine.matches(make(Level.FATAL))
        assert not engine.matches(make(Level.WARN))

    def test_max_level(self):
        engine = FilterEngine(build_spec())
        spec = FilterSpec(max_level=Level.WARN)
        engine.set_spec(spec)
        assert engine.matches(make(Level.INFO))
        assert not engine.matches(make(Level.ERROR))

    def test_level_range(self):
        engine = FilterEngine(FilterSpec(min_level=Level.WARN, max_level=Level.ERROR))
        assert engine.matches(make(Level.ERROR))
        assert not engine.matches(make(Level.CRITICAL))

    def test_invalid_level_is_ignored(self):
        assert build_spec(min_level="NOT_A_LEVEL").min_level is None


class TestTextFilters:
    def test_include_is_anded(self):
        engine = FilterEngine(build_spec(include="timeout, redis"))
        assert engine.matches(make(raw="redis connection timeout"))
        assert not engine.matches(make(raw="redis ok"))
        assert not engine.matches(make(raw="disk timeout"))

    def test_include_is_case_insensitive(self):
        engine = FilterEngine(build_spec(include="REDIS"))
        assert engine.matches(make(raw="redis connection refused"))

    def test_exclude(self):
        engine = FilterEngine(build_spec(exclude="healthz"))
        assert engine.matches(make(raw="GET /api/v1/users"))
        assert not engine.matches(make(raw="GET /healthz"))

    def test_exclude_regex(self):
        engine = FilterEngine(build_spec(exclude_regex=r"^\s*\d{4}-\d{2}-\d{2}"))
        assert engine.matches(make(raw="plain"))
        assert not engine.matches(make(raw="2026-08-30 20:00:01 INFO x"))

    def test_invalid_regex_raises(self):
        with pytest.raises(ValueError, match="invalid exclude regex"):
            build_spec(exclude_regex="[unclosed")

    def test_error_only(self):
        engine = FilterEngine(build_spec(error_only=True))
        assert engine.matches(make(Level.ERROR))
        assert not engine.matches(make(Level.WARN))


class TestSourceFilter:
    def test_exact_source_match(self):
        engine = FilterEngine(build_spec(sources=["app.log"]))
        assert engine.matches(make(source="app.log"))
        assert not engine.matches(make(source="nginx.log"))

    def test_multiple_sources(self):
        engine = FilterEngine(build_spec(sources=["a.log", "b.log"]))
        assert engine.matches(make(source="a.log"))
        assert engine.matches(make(source="b.log"))
        assert not engine.matches(make(source="c.log"))


class TestApply:
    def test_empty_spec_returns_same_list(self):
        entries = [make(), make(Level.ERROR)]
        engine = FilterEngine()
        assert engine.apply(entries) is entries      # no needless copy

    def test_apply_filters(self):
        entries = [make(Level.INFO), make(Level.ERROR), make(Level.CRITICAL)]
        engine = FilterEngine(build_spec(min_level="ERROR"))
        assert len(engine.apply(entries)) == 2

    def test_compose_filters(self, parser):
        entries = [
            parser.parse("2026-08-30 20:00:01 ERROR redis timeout in pool"),
            parser.parse("2026-08-30 20:00:02 ERROR disk failure"),
            parser.parse("2026-08-30 20:00:03 INFO redis warmup"),
        ]
        engine = FilterEngine(build_spec(min_level="ERROR", include="redis"))
        matched = engine.apply(entries)
        assert len(matched) == 1
        assert "redis timeout" in matched[0].raw


class TestSpecDescription:
    def test_empty_description(self):
        assert FilterSpec().describe().startswith("no filters")

    def test_description_lists_criteria(self):
        spec = build_spec(min_level="ERROR", include="redis", exclude="healthz")
        text = spec.describe()
        assert "ERROR" in text
        assert "redis" in text
        assert "healthz" in text

    def test_is_empty(self):
        assert FilterSpec().is_empty()
        assert not build_spec(min_level="ERROR").is_empty()


class TestHighlight:
    def test_finds_spans(self):
        spans = highlight_terms("redis timeout in redis", ("redis",))
        assert spans == [(0, 5), (17, 22)]

    def test_overlaps_removed(self):
        spans = highlight_terms("abcdef", ("abc", "bcd"))
        assert spans == [(0, 3)]

    def test_no_terms(self):
        assert highlight_terms("anything", ()) == []

    def test_case_insensitive(self):
        assert highlight_terms("Redis timeout", ("redis",)) == [(0, 5)]

    def test_ignores_empty_term(self):
        assert highlight_terms("text", ("",)) == []


class TestHighlightSpec:
    """``search`` drives highlighting only - it must never hide lines."""

    def test_search_does_not_filter(self):
        engine = FilterEngine(build_spec(search="timeout"))
        # The spec still reports empty, so `apply` returns the input untouched.
        assert engine.spec.is_empty()
        entries = [make(raw="a timeout happened"), make(raw="nothing here")]
        assert engine.apply(entries) is entries

    def test_search_terms_are_parsed_and_lowercased(self):
        spec = build_spec(search="Timeout, DEADLOCK")
        assert spec.search == ("timeout", "deadlock")

    def test_describe_mentions_highlight(self):
        assert "highlighting" in build_spec(search="redis").describe()

    def test_describe_combines_with_filters(self):
        spec = build_spec(min_level="ERROR", search="redis")
        text = spec.describe()
        assert "ERROR" in text and "redis" in text


class TestV1Compatibility:
    """The mutable ``set_*`` accessors from v1 must keep working."""

    def test_set_level_filter_accepts_name(self):
        engine = FilterEngine()
        engine.set_level_filter("ERROR")
        assert engine.spec.min_level is Level.ERROR

    def test_set_level_filter_accepts_enum(self):
        engine = FilterEngine()
        engine.set_level_filter(Level.WARN)
        assert engine.spec.min_level is Level.WARN

    def test_set_level_filter_none_clears(self):
        engine = FilterEngine(build_spec(min_level="ERROR"))
        engine.set_level_filter(None)
        assert engine.spec.min_level is None

    def test_set_level_filter_rejects_garbage(self):
        engine = FilterEngine()
        engine.set_level_filter("NOT_A_LEVEL")
        assert engine.spec.min_level is None

    def test_set_text_filter(self):
        engine = FilterEngine()
        engine.set_text_filter("  Timeout  ")
        assert engine.spec.include == ("timeout",)

    def test_set_text_filter_none_clears(self):
        engine = FilterEngine(build_spec(include="a"))
        engine.set_text_filter(None)
        assert engine.spec.include == ()

    def test_set_source_filter(self):
        engine = FilterEngine()
        engine.set_source_filter("app.log")
        assert engine.spec.sources == frozenset({"app.log"})
        assert engine.matches(make(source="app.log"))
        assert not engine.matches(make(source="other.log"))

    def test_add_exclude_pattern(self):
        engine = FilterEngine()
        engine.add_exclude_pattern(r"^\d{4}-\d{2}")
        assert engine.matches(make(raw="plain message"))
        assert not engine.matches(make(raw="2026-08-30 20:00:01 INFO x"))

    def test_add_exclude_pattern_is_additive(self):
        engine = FilterEngine()
        engine.add_exclude_pattern("healthz")
        engine.add_exclude_pattern("metrics")
        assert len(engine.spec.exclude_regex) == 2
        assert not engine.matches(make(raw="GET /healthz"))
        assert not engine.matches(make(raw="GET /metrics"))

    def test_add_exclude_pattern_rejects_invalid(self):
        engine = FilterEngine()
        with pytest.raises(ValueError, match="invalid exclude regex"):
            engine.add_exclude_pattern("[unclosed")

    def test_clear_filters(self):
        engine = FilterEngine()
        engine.set_level_filter("ERROR")
        engine.set_text_filter("timeout")
        engine.set_source_filter("app.log")
        engine.add_exclude_pattern("healthz")
        engine.clear_filters()
        assert engine.spec.is_empty()
        assert engine.matches(make(Level.INFO, source="anything"))

    def test_setters_do_not_mutate_the_shared_spec(self):
        original = build_spec(min_level="ERROR")
        engine = FilterEngine(original)
        engine.set_level_filter(None)
        # The caller's dataclass must be untouched: setters copy, not mutate.
        assert original.min_level is Level.ERROR


class TestPerformance:
    def test_matching_is_cheap_for_many_entries(self):
        """Regression guard: no per-call level re-parsing or repeated lowering."""
        import time

        entries = [make(Level.INFO, raw=f"INFO message number {i} redis timeout")
                   for i in range(10_000)]
        engine = FilterEngine(build_spec(min_level="WARN", include="redis"))

        start = time.perf_counter()
        for entry in entries:
            engine.matches(entry)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.1, f"10k matches took {elapsed:.3f}s"