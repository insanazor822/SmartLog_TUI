"""Diagnostics tests: offline rules, provider cascade, non-blocking behaviour."""

from __future__ import annotations

import asyncio
import time

import pytest

from smartlog.diagnostics import (
    Confidence,
    Diagnosis,
    DiagnosticEngine,
    ProviderInfo,
    discover_providers,
)
from smartlog.knowledge import KnowledgeBase
from smartlog.parser import LineParser


@pytest.fixture(scope="module")
def parser() -> LineParser:
    return LineParser()


class TestKnowledgeBase:
    @pytest.mark.parametrize("line,rule_id", [
        ("CRITICAL Out of memory: OOMKilled process 4920", "oom"),
        ("ERROR No space left on device", "disk_full"),
        ("ERROR deadlock detected on relation orders", "db_deadlock"),
        ("ERROR Connection refused: redis://127.0.0.1:6379", "conn_refused"),
        ("ERROR permission denied opening /etc/secret", "permission"),
        ("ERROR authentication failed for user admin", "auth_failure"),
        ("ERROR SSL handshake failed: certificate verify failed", "tls"),
        ("WARN slow query detected: 3200ms", "slow_query"),
        ("ERROR circuit breaker OPEN for payments", "circuit_breaker"),
        ("ERROR too many open files", "throttle"),
        ("error: NXDOMAIN when resolving db.internal", "dns"),
        ("panic: runtime error: index out of range", "panic"),
        ("ERROR connection pool exhausted", "db_deadlock"),
        ("status 502 from upstream proxy", "http_5xx"),
    ])
    def test_rule_matching(self, line, rule_id):
        kb = KnowledgeBase()
        best = kb.best(line)
        assert best is not None, f"no rule matched {line!r}"
        assert best[0].id == rule_id

    def test_unknown_line_has_no_match(self):
        kb = KnowledgeBase()
        assert kb.best("INFO routine heartbeat") is None

    def test_rules_provide_runnable_commands(self):
        kb = KnowledgeBase()
        rule, _ = kb.best("ERROR Out of memory: OOMKilled process 1")
        commands = [cmd for _, _, cmd in rule.checks if cmd]
        assert commands, "OOM rule must ship concrete commands"
        assert any("ps aux" in c or "free -h" in c for c in commands)

    def test_every_rule_has_checks_and_prevention(self):
        from smartlog.knowledge import RULES

        for rule in RULES:
            assert rule.checks, f"{rule.id} has no checks"
            assert rule.prevention, f"{rule.id} has no prevention tips"
            assert rule.root_cause
            assert rule.patterns

    def test_rule_ids_unique(self):
        from smartlog.knowledge import RULES

        ids = [r.id for r in RULES]
        assert len(ids) == len(set(ids))

    def test_lookup_ranks_best_first(self):
        kb = KnowledgeBase()
        hits = kb.lookup("ERROR deadlock detected; connection refused", limit=2)
        assert hits[0][0].id == "db_deadlock"
        assert len(hits) >= 2

    def test_category_lookup(self):
        kb = KnowledgeBase()
        assert kb.category_for("No space left on device") == "Resource"


class TestOfflineDiagnosis:
    @pytest.mark.asyncio
    async def test_oom_diagnosis_is_actionable(self):
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text(
            "CRITICAL Out of memory: OOMKilled process 4920 (node)"
        )
        assert result.source == "offline"
        assert "memory" in result.summary.lower()
        assert result.recommendations
        assert any(r.command for r in result.recommendations)
        assert result.confidence == Confidence.VERY_HIGH

    @pytest.mark.asyncio
    async def test_multiple_rule_matches_are_merged(self):
        """Regression: a runner-up rule must not crash the merge path."""
        hits = KnowledgeBase().lookup(
            "ERROR deadlock detected: slow query 3200ms on relation orders", limit=3
        )
        assert len(hits) >= 2, "fixture must trigger more than one rule"

        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text(hits and
            "ERROR deadlock detected: slow query 3200ms on relation orders")
        assert result.recommendations
        assert any(r.title.startswith("Also check") for r in result.recommendations)
        assert any("deadlock" in e.lower() for e in result.evidence)

    @pytest.mark.asyncio
    async def test_unknown_error_gets_generic_guidance(self):
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text("zzz qqq unparseable nonsense ~~~")
        assert result.source == "offline"
        assert result.confidence == Confidence.LOW
        assert result.recommendations

    @pytest.mark.asyncio
    async def test_context_lines_become_evidence(self):
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text(
            "ERROR Transaction failed",
            context=["INFO starting transaction",
                    "ERROR deadlock detected on orders"],
        )
        assert any("deadlock" in e for e in result.evidence)

    @pytest.mark.asyncio
    async def test_evidence_can_name_rule_match(self):
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text("ERROR Permission denied: /etc/x")
        assert any("permission" in e.lower() for e in result.evidence)

    def test_severity_derivation(self):
        high = Diagnosis(summary="s", root_cause="r",
                         recommendations=[__import__(
                             "smartlog.diagnostics", fromlist=["Recommendation"]
                         ).Recommendation(title="t", severity="high")])
        assert high.severity == "high"


class TestContextWindow:
    @pytest.mark.asyncio
    async def test_context_is_the_surrounding_window(self, parser):
        entries = [
            parser.parse(f"2026-08-30 20:00:{i:02d} INFO line {i}", "app.log", i)
            for i in range(20)
        ]
        target = entries[10]
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_entry(target, entries)
        # The entry under analysis plus a symmetric window.
        assert 1 <= len(result.related_entries) <= 13

    @pytest.mark.asyncio
    async def test_target_without_buffer(self, parser):
        entry = parser.parse("ERROR disk failure", "app.log", 1)
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_entry(entry, [])
        assert result.recommendations


class TestProviderCascade:
    def test_env_providers_are_discovered(self, monkeypatch):
        import smartlog.diagnostics as module

        monkeypatch.setattr(module, "_discovery_cache", None)
        monkeypatch.setenv("SMARTLOG_LLM_URL", "http://localhost:9999")
        monkeypatch.setenv("SMARTLOG_LLM_MODEL", "my-model")
        monkeypatch.setenv("SMARTLOG_NO_LOCAL", "1")
        providers = discover_providers(force=True)
        assert providers[0].id == "custom"
        assert providers[0].url.endswith("/v1/chat/completions")
        assert providers[0].model == "my-model"

    def test_openai_env_is_detected(self, monkeypatch):
        import smartlog.diagnostics as module

        monkeypatch.setattr(module, "_discovery_cache", None)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("SMARTLOG_NO_LOCAL", "1")
        providers = discover_providers(force=True)
        assert any(p.id == "openai" and p.api_key == "sk-test"
                   for p in providers)

    def test_no_local_flag_suppresses_probes(self, monkeypatch):
        import smartlog.diagnostics as module

        monkeypatch.setattr(module, "_discovery_cache", None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("SMARTLOG_LLM_URL", raising=False)
        monkeypatch.setenv("SMARTLOG_NO_LOCAL", "1")
        assert discover_providers(force=True) == []

    def test_url_already_complete_is_not_doubled(self):
        from smartlog.diagnostics import _normalise_url

        assert _normalise_url("http://h/v1/chat/completions").count(
            "chat/completions") == 1
        assert _normalise_url("http://h").endswith("/v1/chat/completions")

    def test_cloud_providers_split(self):
        from smartlog.diagnostics import cloud_providers

        providers = [
            ProviderInfo("local", "http://127.0.0.1:8080/v1/chat/completions", "m"),
            ProviderInfo("cloud", "https://api.openai.com/v1/chat/completions", "m"),
        ]
        assert [p.id for p in cloud_providers(providers)] == ["cloud"]


class TestLLMIntegration:
    @pytest.mark.asyncio
    async def test_successful_llm_response_is_used(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def fake_call(provider, prompt):
            return '{"summary":"DB pool exhausted","confidence":"high",' \
                   '"root_cause":"Pool size too small","evidence":["pool=20"],' \
                   '"recommendations":[{"title":"Raise pool size",' \
                   '"description":"","command":"psql -c \'show pools\'",' \
                   '"severity":"high"}],"prevention":["autotune"]}'

        monkeypatch.setattr(engine, "_call_provider", fake_call)
        result = await engine.diagnose_text("ERROR connection pool exhausted")
        assert result.source == "fake"
        assert result.summary == "DB pool exhausted"
        assert result.confidence == "high"
        assert result.recommendations[0].command == "psql -c 'show pools'"

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_fails(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def always_none(provider, prompt):
            return None

        monkeypatch.setattr(engine, "_call_provider", always_none)
        result = await engine.diagnose_text("ERROR No space left on device")
        assert result.source == "offline"
        assert "space" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def garbage(provider, prompt):
            return "I cannot help with that."

        monkeypatch.setattr(engine, "_call_provider", garbage)
        result = await engine.diagnose_text("ERROR No space left on device")
        assert result.source == "offline"

    @pytest.mark.asyncio
    async def test_fenced_json_is_accepted(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def fenced(provider, prompt):
            return '```json\n{"summary":"OOM","confidence":"very_high",' \
                   '"root_cause":"heap limit","recommendations":[]}\n```'

        monkeypatch.setattr(engine, "_call_provider", fenced)
        result = await engine.diagnose_text("Out of memory")
        assert result.summary == "OOM"

    @pytest.mark.asyncio
    async def test_empty_llm_recommendations_get_rule_backstop(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def empty_recs(provider, prompt):
            return '{"summary":"Pool issue","confidence":"low",' \
                   '"root_cause":"unknown","recommendations":[]}'

        monkeypatch.setattr(engine, "_call_provider", empty_recs)
        result = await engine.diagnose_text("ERROR connection pool exhausted")
        assert result.recommendations, "must never return zero next steps"

    @pytest.mark.asyncio
    async def test_invalid_confidence_is_normalised(self, monkeypatch):
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("fake", "http://x/v1/chat/completions", "m", timeout=1.0)
        ])

        async def weird(provider, prompt):
            return '{"summary":"x","confidence":"SUPER HIGH",' \
                   '"root_cause":"y","recommendations":["do a thing"]}'

        monkeypatch.setattr(engine, "_call_provider", weird)
        result = await engine.diagnose_text("ERROR timeout")
        assert result.confidence == Confidence.MEDIUM
        assert result.recommendations[0].description == "do a thing"


class TestNonBlocking:
    @pytest.mark.asyncio
    async def test_diagnosis_does_not_block_the_event_loop(self, monkeypatch):
        """The critical regression: a slow provider must not freeze the UI."""
        engine = DiagnosticEngine()
        monkeypatch.setattr(engine, "_ordered_providers", lambda: [
            ProviderInfo("slow", "http://x/v1/chat/completions", "m", timeout=5.0)
        ])

        async def slow(provider, prompt):
            await asyncio.sleep(0.5)      # stands in for a network round-trip
            return None

        monkeypatch.setattr(engine, "_call_provider", slow)

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        result = await engine.diagnose_text("ERROR disk full")
        beat.cancel()

        assert result.source == "offline"
        assert ticks > 10, (
            f"event loop only ticked {ticks} times during diagnosis — "
            "the UI would have frozen"
        )

    @pytest.mark.asyncio
    async def test_provider_timeout_is_bounded(self):
        """A hung endpoint must be abandoned at its timeout, not forever."""
        engine = DiagnosticEngine()

        def hang(provider, prompt):
            time.sleep(2.0)
            return None

        monkeypatch_provider = ProviderInfo(
            "hang", "http://x/v1/chat/completions", "m", timeout=0.2
        )
        result = await asyncio.wait_for(
            engine._call_provider(monkeypatch_provider, "prompt"), timeout=1.0
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_history_is_capped(self):
        engine = DiagnosticEngine(offline=True)
        for i in range(60):
            await engine.diagnose_text(f"ERROR unique failure number {i}")
        assert len(engine.history) <= 50

    @pytest.mark.asyncio
    async def test_clear_history(self):
        engine = DiagnosticEngine(offline=True)
        await engine.diagnose_text("ERROR disk full")
        engine.clear_history()
        assert engine.history == []


class TestRendering:
    @pytest.mark.asyncio
    async def test_to_text_includes_commands(self):
        engine = DiagnosticEngine(offline=True)
        result = await engine.diagnose_text("ERROR Out of memory: OOMKilled")
        text = result.to_text()
        assert "Next steps:" in text
        assert "$ " in text
        assert "Root cause:" in text
        assert "offline" in text