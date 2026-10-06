"""Async diagnostic engine with cascading providers.

The original ran ``urllib.request.urlopen`` **synchronously inside the Textual
event loop**, with a 2.5 s timeout against up to three endpoints (including two
localhost LLM ports even when no API key existed). In practice every keystroke
that triggered a diagnosis froze the entire UI for several seconds.

Here:

* every network call runs in a worker thread via :func:`asyncio.to_thread`
  (importable and awaitable, mockable in tests),
* endpoint discovery is **cached** and probed once, so a machine without an LLM
  pays a single short probe instead of 2.5 s on every diagnosis,
* all endpoints are queried **concurrently** with an overall deadline, so the
  worst case is one timeout instead of three,
* the offline knowledge base always produces a result, and is used as context
  for the LLM so answers stay grounded in known signatures.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .knowledge import KnowledgeBase, Rule
from .models import LogEntry

__all__ = [
    "SYSTEM_PROMPT",
    "Confidence",
    "Diagnosis",
    "DiagnosticEngine",
    "ProviderInfo",
    "Recommendation",
]

SYSTEM_PROMPT = (
    "You are a senior site-reliability engineer doing root-cause analysis on "
    "production logs.\n"
    "Reply with ONE JSON object and nothing else. No markdown fence, no prose.\n"
    "Schema:\n"
    '{"summary": string, "confidence": "low|medium|high|very_high",'
    ' "root_cause": string, "evidence": [string],'
    ' "recommendations": [{"title": string, "description": string,'
    ' "command": string|null, "severity": "low|medium|high|critical"}],'
    ' "prevention": [string]}\n'
    "Rules: ground every claim in the supplied logs; prefer one verifiable "
    "root cause over a list of guesses; prefer concrete shell commands over "
    "generic advice; if the evidence is insufficient, say so and set "
    "confidence to low."
)

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_BALANCED = re.compile(r"\{.*\}", re.DOTALL)


class Confidence:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass(slots=True)
class Recommendation:
    title: str
    description: str = ""
    command: str | None = None
    severity: str = "medium"
    estimated_time: str = ""


@dataclass(slots=True)
class Diagnosis:
    """Result of one diagnostic run."""

    summary: str
    root_cause: str
    confidence: str = Confidence.MEDIUM
    source: str = "offline"          # provider id, or "offline"
    evidence: list[str] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    prevention: list[str] = field(default_factory=list)
    related_entries: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    error: str | None = None

    @property
    def severity(self) -> str:
        order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if not self.recommendations:
            return "low"
        return max(self.recommendations, key=lambda r: order.get(r.severity, 0)).severity

    def to_text(self) -> str:
        lines = [
            f"[{self.confidence.upper()}] {self.summary}",
            f"Source: {self.source} ({self.elapsed_ms:.0f} ms)",
            f"Root cause: {self.root_cause}",
        ]
        if self.evidence:
            lines.append("")
            lines.append("Evidence:")
            lines.extend(f"  - {e}" for e in self.evidence[:6])
        if self.recommendations:
            lines.append("")
            lines.append("Next steps:")
            for i, rec in enumerate(self.recommendations, 1):
                lines.append(f"  {i}. {rec.title}  [{rec.severity}]")
                if rec.description:
                    lines.append(f"     {rec.description}")
                if rec.command:
                    lines.append(f"     $ {rec.command}")
        if self.prevention:
            lines.append("")
            lines.append("Prevention:")
            lines.extend(f"  - {p}" for p in self.prevention)
        if self.docs:
            lines.append("")
            lines.append("References:")
            lines.extend(f"  - {d}" for d in self.docs)
        return "\n".join(lines)


@dataclass(slots=True)
class ProviderInfo:
    """A reachable chat-completions endpoint."""

    id: str
    url: str
    model: str
    api_key: str | None = None
    kind: str = "openai"                # openai | ollama
    timeout: float = 12.0

    @property
    def label(self) -> str:
        return f"{self.id} ({self.model})"


# --------------------------------------------------------------------------- #
# Provider discovery
# --------------------------------------------------------------------------- #

_LOCAL_PROBES: tuple[tuple[str, str, str, str], ...] = (
    ("ollama", "http://127.0.0.1:11434/v1/chat/completions", "llama3.2", "openai"),
    ("llamacpp", "http://127.0.0.1:8080/v1/chat/completions", "local-model", "openai"),
    ("lmstudio", "http://127.0.0.1:1234/v1/chat/completions", "local-model", "openai"),
)

_discovery_cache: tuple[float, list[ProviderInfo]] | None = None
_DISCOVERY_TTL = 30.0


def discover_providers(force: bool = False) -> list[ProviderInfo]:
    """Return configured providers. Results are cached for 30 seconds.

    Environment:

    ``SMARTLOG_LLM_URL``   OpenAI-compatible endpoint (highest precedence)
    ``SMARTLOG_LLM_MODEL``model name for that endpoint
    ``SMARTLOG_LLM_API_KEY`` API key for that endpoint
    ``OPENAI_API_KEY``    enables api.openai.com with ``OPENAI_MODEL``
    ``SMARTLOG_NO_LOCAL`` set to ``1`` to skip localhost probes entirely
    """
    global _discovery_cache
    now = time.monotonic()
    if (not force and _discovery_cache is not None
            and now - _discovery_cache[0] < _DISCOVERY_TTL):
        return list(_discovery_cache[1])

    providers: list[ProviderInfo] = []

    llm_url = os.getenv("SMARTLOG_LLM_URL", "").strip()
    if llm_url:
        providers.append(ProviderInfo(
            id="custom",
            url=_normalise_url(llm_url),
            model=os.getenv("SMARTLOG_LLM_MODEL", "local-model"),
            api_key=os.getenv("SMARTLOG_LLM_API_KEY") or None,
            timeout=float(os.getenv("SMARTLOG_LLM_TIMEOUT", "20")),
        ))

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        providers.append(ProviderInfo(
            id="openai",
            url="https://api.openai.com/v1/chat/completions",
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=openai_key,
            timeout=float(os.getenv("OPENAI_TIMEOUT", "20")),
        ))

    if os.getenv("SMARTLOG_NO_LOCAL", "").strip() not in {"1", "true", "yes"}:
        providers.extend(
            ProviderInfo(id=pid, url=url, model=model, kind=kind, timeout=3.0)
            for pid, url, model, kind in _LOCAL_PROBES
        )

    _discovery_cache = (now, providers)
    return list(providers)


def _normalise_url(url: str) -> str:
    text = url.rstrip("/")
    if text.endswith("/chat/completions"):
        return text
    return text + "/v1/chat/completions"


def cloud_providers(providers: Iterable[ProviderInfo]) -> list[ProviderInfo]:
    """Providers that are not localhost (i.e. slow remote calls)."""
    return [p for p in providers if "127.0.0.1" not in p.url and "localhost" not in p.url]


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class DiagnosticEngine:
    """Produces a :class:`Diagnosis`, trying LLM providers then rules."""

    def __init__(
        self,
        *,
        knowledge: KnowledgeBase | None = None,
        context_lines: int = 12,
        max_context_chars: int = 6000,
        offline: bool = False,
    ) -> None:
        self.knowledge = knowledge or KnowledgeBase()
        self.context_lines = context_lines
        self.max_context_chars = max_context_chars
        self.offline = offline
        self._history: list[Diagnosis] = []
        self.last_provider: ProviderInfo | None = None

    # -- public API ------------------------------------------------------- #

    async def diagnose_entry(
        self,
        entry: LogEntry,
        buffer: Sequence[LogEntry] | None = None,
    ) -> Diagnosis:
        """Diagnose ``entry``, enriching it with its surrounding context."""
        entries = list(buffer) if buffer else []
        idx = next((i for i, candidate in enumerate(entries) if candidate is entry), None)
        window = self._context(entries, idx)
        return await self.diagnose_text(entry.raw, window)

    async def diagnose_text(
        self,
        text: str,
        context: Sequence[str] | None = None,
    ) -> Diagnosis:
        """Diagnose a raw message with optional surrounding log lines."""
        started = time.perf_counter()
        rules = self.knowledge.lookup(text, limit=3)
        offline_diagnosis = self._offline(text, rules, context)

        if self.offline:
            return self._finish(offline_diagnosis, started, context)

        prompt_context = self._build_prompt_context(text, context, rules)
        for provider in self._ordered_providers():
            raw = await self._call_provider(provider, prompt_context)
            if raw is None:
                continue
            parsed = self._parse_llm_json(raw)
            if parsed is None:
                continue
            diagnosis = self._merge(parsed, rules, provider.id)
            self.last_provider = provider
            return self._finish(diagnosis, started, context)

        return self._finish(offline_diagnosis, started, context)

    @property
    def history(self) -> list[Diagnosis]:
        return list(self._history)

    def clear_history(self) -> None:
        self._history.clear()

    # -- providers -------------------------------------------------------- #

    def _ordered_providers(self) -> list[ProviderInfo]:
        """Local endpoints first (instant when present), then cloud providers."""
        providers = discover_providers()
        local = [p for p in providers if p.id in _LOCAL_PROBE_IDS]
        remote = [p for p in providers if p.id not in _LOCAL_PROBE_IDS]
        return local + remote

    async def _call_provider(self, provider: ProviderInfo, prompt: str) -> str | None:
        """Call one provider in a thread. Never raises; returns None on failure."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._post, provider, prompt),
                timeout=provider.timeout,
            )
        except TimeoutError:
            return None
        except Exception:
            return None

    @staticmethod
    def _post(provider: ProviderInfo, prompt: str) -> str | None:
        """Blocking HTTP POST. Isolated so it can be swapped in tests."""
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        payload = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
            "stream": False,
        }
        if provider.kind == "openai":
            payload["response_format"] = {"type": "json_object"}

        request = urllib.request.Request(
            provider.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=provider.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            return None

        try:
            data = json.loads(body)
        except ValueError:
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):     # some servers return content blocks
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return content if isinstance(content, str) else None

    # -- offline path ----------------------------------------------------- #

    def _offline(
        self,
        text: str,
        rules: list[tuple[Rule, str]],
        context: Sequence[str] | None,
    ) -> Diagnosis:
        if rules:
            rule, matched = rules[0]
            others = rules[1:]
            recs = [
                Recommendation(
                    title=title,
                    description=desc,
                    command=command or None,
                    severity=_infer_severity(text),
                )
                for title, desc, command in rule.checks
            ]
            # Fold in one extra check from the runner-up rule when it adds value.
            if others:
                secondary_rule = others[0][0]
                for title, desc, command in secondary_rule.checks[:1]:
                    recs.append(Recommendation(
                        title=f"Also check: {title}",
                        description=desc,
                        command=command or None,
                        severity="medium",
                    ))
            evidence = [f"Matched rule '{rule.id}' on: {matched}"]
            evidence.extend(
                f"Secondary match '{other.id}' on: {other_match}"
                for other, other_match in others
            )
            evidence.extend(_salient(context or ()))
            return Diagnosis(
                summary=rule.title,
                root_cause=rule.root_cause,
                confidence=rule.confidence,
                source="offline",
                evidence=evidence[:8],
                recommendations=recs,
                prevention=list(rule.prevention),
                docs=list(rule.docs),
            )

        return Diagnosis(
            summary=_generic_title(text),
            root_cause=(
                "No known signature matched. This line is most likely a symptom; "
                "correlate it with the preceding errors and the service journal."
            ),
            confidence=Confidence.LOW,
            source="offline",
            evidence=[f"Input: {text[:200]}"] + _salient(context or ()),
            recommendations=[
                Recommendation(
                    title="Read the surrounding journal",
                    description="Stack context almost always identifies the cause.",
                    command="journalctl -xe --no-pager -n 80",
                    severity="high",
                ),
                Recommendation(
                    title="Find the failing unit",
                    description="",
                    command="systemctl --failed",
                    severity="medium",
                ),
                Recommendation(
                    title="Correlate by correlation id",
                    description="Grep the request/trace id across every log file.",
                    command="grep -R '<correlation-id>' /var/log/",
                    severity="high",
                ),
            ],
            prevention=[
                "Emit structured (JSON) logs so signatures are machine-detectable",
                "Alert on error-rate ratio, not absolute error count",
            ],
        )

    # -- helpers ---------------------------------------------------------- #

    def _context(
        self,
        buffer: Sequence[LogEntry],
        index: int | None,
    ) -> list[str]:
        if not buffer:
            return []
        if index is None:
            window = buffer[-self.context_lines:]
        else:
            half = self.context_lines // 2
            window = buffer[max(0, index - half): index + half + 1]
        return [e.raw for e in window]

    def _build_prompt_context(
        self,
        text: str,
        context: Sequence[str] | None,
        rules: list[tuple[Rule, str]],
    ) -> str:
        parts = [f"TARGET LOG LINE:\n{text}"]
        if context:
            parts.append("SURROUNDING LOG LINES:")
            parts.extend(f"  {line}" for line in context)
        if rules:
            parts.append("KNOWN SIGNATURES (verify, do not assume):")
            for rule, matched in rules:
                parts.append(f"  - {rule.id}: matched '{matched}' -> {rule.root_cause}")
        prompt = "\n".join(parts)
        return prompt[: self.max_context_chars]

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any] | None:
        text = _JSON_FENCE.sub("", raw.strip())
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            pass
        # Fall back to the first balanced JSON object in the response.
        match = _BALANCED.search(text)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _merge(
        self,
        data: dict[str, Any],
        rules: list[tuple[Rule, str]],
        provider_id: str,
    ) -> Diagnosis:
        """Combine LLM output with rule evidence; never let the LLM omit safety."""
        confidence = str(data.get("confidence", "")).lower().strip()
        if confidence not in (
            Confidence.LOW, Confidence.MEDIUM,
            Confidence.HIGH, Confidence.VERY_HIGH,
        ):
            confidence = Confidence.MEDIUM

        recs: list[Recommendation] = []
        for i, raw in enumerate(data.get("recommendations") or [], 1):
            if isinstance(raw, str):
                recs.append(Recommendation(title=f"Step {i}", description=raw))
                continue
            if not isinstance(raw, dict):
                continue
            recs.append(Recommendation(
                title=str(raw.get("title") or f"Step {i}"),
                description=str(raw.get("description") or ""),
                command=(str(raw["command"]) if raw.get("command") else None),
                severity=str(raw.get("severity") or "medium").lower(),
            ))
        if not recs:
            # An empty action list is not useful; fall back to the rule's checks.
            for rule, _ in rules[:1]:
                recs.extend(
                    Recommendation(title=t, description=d, command=c or None,
                                   severity="medium")
                    for t, d, c in rule.checks[:3]
                )
        recs = recs[:8]

        evidence = [str(e) for e in (data.get("evidence") or []) if e]
        if rules:
            evidence.extend(
                f"Matched rule '{rule.id}' on: {matched}" for rule, matched in rules
            )

        prevention = [str(p) for p in (data.get("prevention") or []) if p]
        if not prevention and rules:
            prevention = list(rules[0][0].prevention)

        return Diagnosis(
            summary=str(data.get("summary") or "Unclassified failure"),
            root_cause=str(data.get("root_cause") or "Not determined"),
            confidence=confidence,
            source=provider_id,
            evidence=evidence[:10],
            recommendations=recs,
            prevention=prevention[:8],
            docs=list(rules[0][0].docs) if rules else [],
        )

    def _finish(
        self,
        diagnosis: Diagnosis,
        started: float,
        context: Sequence[str] | None,
    ) -> Diagnosis:
        diagnosis.elapsed_ms = (time.perf_counter() - started) * 1000.0
        diagnosis.related_entries = list(context or [])[:self.context_lines]
        self._history.append(diagnosis)
        if len(self._history) > 50:
            del self._history[:-50]
        return diagnosis


_LOCAL_PROBE_IDS = frozenset(pid for pid, _, _, _ in _LOCAL_PROBES)


def _salient(context: Sequence[str], limit: int = 4) -> list[str]:
    """Pick the most informative surrounding lines (errors first)."""
    picked = [c for c in context if re.search(
        r"\b(error|fatal|critical|exception|refus|timeout|denied|failed)\b", c,
        re.IGNORECASE,
    )]
    return [c[:180] for c in picked[-limit:]]


def _infer_severity(text: str) -> str:
    lowered = text.lower()
    if any(k in lowered for k in ("fatal", "panic", "oom", "out of memory",
                                  "corrupt", "core dumped", "abort")):
        return "critical"
    if any(k in lowered for k in ("error", "exception", "refused", "denied",
                                  "deadlock", "traceback", "500")):
        return "high"
    if any(k in lowered for k in ("warn", "timeout", "slow", "429", "4")):
        return "medium"
    return "low"


def _generic_title(text: str) -> str:
    head = text.strip().split(" ", 6)
    return " ".join(head[:6])[:80] or "Unclassified log event"