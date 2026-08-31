"""
AI Diagnostics Module for SmartLog TUI
Provides intelligent error analysis and recommended fixes with dynamic LLM & local fallback support.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DiagnosisConfidence(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass
class RecommendedFix:
    """A recommended fix for an error."""

    title: str
    description: str
    command: str | None = None
    file_path: str | None = None
    severity: str = "medium"
    estimated_time: str = "5 min"


@dataclass
class Diagnosis:
    """Complete diagnosis for an error."""

    error_summary: str
    root_cause: str
    confidence: DiagnosisConfidence
    recommendations: list[RecommendedFix] = field(default_factory=list)
    related_logs: list[str] = field(default_factory=list)
    prevention_tips: list[str] = field(default_factory=list)
    documentation_links: list[str] = field(default_factory=list)


class ErrorKnowledgeBase:
    """
    Local knowledge base for error diagnosis fallback.
    Used when external/local LLM services are unreachable.
    """

    # Error patterns and their diagnoses
    KNOWLEDGE_BASE: dict[str, dict[str, Any]] = {
        # HTTP Errors
        r'500\s+Internal\s+Server': {
            'summary': 'Internal Server Error',
            'root_cause': 'Server encountered an unexpected condition that prevented it from fulfilling the request.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Application Logs',
                    description='Review application logs for stack traces and error details.',
                    command='tail -f /var/log/application/error.log',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Database Connection',
                    description='Verify database connectivity and query execution.',
                    command='systemctl status postgresql',
                    severity='medium',
                ),
                RecommendedFix(
                    title='Review Recent Deployments',
                    description='Check if recent code changes introduced the issue.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement comprehensive error handling',
                'Set up proper logging and monitoring',
                'Use health check endpoints',
            ],
        },
        r'502\s+Bad\s+Gateway': {
            'summary': 'Bad Gateway Error',
            'root_cause': 'Server acting as a gateway received an invalid response from upstream server.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Upstream Service',
                    description='Verify that the upstream service is running and healthy.',
                    command='curl -I http://upstream-service:port/health',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Proxy Configuration',
                    description='Review proxy/gateway configuration for upstream settings.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement circuit breakers',
                'Set appropriate timeouts',
                'Use health checks for upstream services',
            ],
        },
        r'503\s+Service\s+Unavailable': {
            'summary': 'Service Unavailable',
            'root_cause': 'Server is temporarily unable to handle the request, often due to maintenance or overload.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Server Load',
                    description='Monitor CPU, memory, and connection limits.',
                    command='top -bn1 | head -20',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check for Maintenance',
                    description='Verify if maintenance mode is enabled.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement proper scaling',
                'Set up load balancing',
                'Configure connection limits appropriately',
            ],
        },
        r'404\s+Not\s+Found': {
            'summary': 'Resource Not Found',
            'root_cause': 'The requested resource could not be found on the server.',
            'confidence': DiagnosisConfidence.VERY_HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Verify URL Path',
                    description='Check if the URL path is correct and the resource exists.',
                    severity='medium',
                ),
                RecommendedFix(
                    title='Check Routing Configuration',
                    description='Review application routing and URL mappings.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement proper URL validation',
                'Set up custom 404 pages',
                'Use URL rewriting for clean paths',
            ],
        },
        # Connection Errors
        r'connection\s+refused': {
            'summary': 'Connection Refused',
            'root_cause': 'The target server actively refused the connection, usually because no service is listening.',
            'confidence': DiagnosisConfidence.VERY_HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Service Status',
                    description='Verify the target service is running.',
                    command='systemctl status <service-name>',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Port Binding',
                    description='Verify the service is listening on the expected port.',
                    command='netstat -tlnp | grep <port>',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Firewall Rules',
                    description='Verify firewall is not blocking the connection.',
                    command='sudo ufw status',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement service health monitoring',
                'Use service dependencies in systemd',
                'Set up proper startup ordering',
            ],
        },
        r'connection\s+timeout': {
            'summary': 'Connection Timeout',
            'root_cause': 'Connection attempt exceeded the timeout period, indicating network issues or unresponsive server.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Network Connectivity',
                    description='Test basic network connectivity to the target.',
                    command='ping -c 4 <target>',
                    severity='high',
                ),
                RecommendedFix(
                    title='Increase Timeout Values',
                    description='Consider increasing timeout settings if the operation is legitimately slow.',
                    severity='low',
                ),
            ],
            'prevention_tips': [
                'Implement retry logic with exponential backoff',
                'Use connection pooling',
                'Monitor network latency',
            ],
        },
        # Memory Errors
        r'out\s+of\s+memory|oomkilled': {
            'summary': 'Out of Memory Error',
            'root_cause': 'The system or container ran out of available memory to complete the operation.',
            'confidence': DiagnosisConfidence.VERY_HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Memory Usage',
                    description='Monitor current memory usage and identify memory-hungry processes.',
                    command='free -h && ps aux --sort=-%mem | head -10',
                    severity='critical',
                ),
                RecommendedFix(
                    title='Check for Memory Leaks',
                    description='Review application code for potential memory leaks.',
                    severity='high',
                ),
                RecommendedFix(
                    title='Increase System Memory',
                    description='Consider adding more RAM or increasing container memory limits.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement memory profiling',
                'Set memory limits for containers',
                'Use memory-efficient data structures',
            ],
        },
        # Disk Errors
        r'no\s+space\s+left\s+on\s+device': {
            'summary': 'Disk Full Error',
            'root_cause': 'The filesystem has run out of available space.',
            'confidence': DiagnosisConfidence.VERY_HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Disk Usage',
                    description='Identify which partitions are full and what is consuming space.',
                    command='df -h && du -sh /* 2>/dev/null | sort -hr | head -10',
                    severity='critical',
                ),
                RecommendedFix(
                    title='Clean Up Old Logs',
                    description='Remove or rotate old log files.',
                    command='journalctl --vacuum-time=7d',
                    severity='medium',
                ),
                RecommendedFix(
                    title='Find Large Files',
                    description='Identify and remove unnecessary large files.',
                    command='find / -type f -size +100M -exec ls -lh {} \\; 2>/dev/null | head -20',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement log rotation',
                'Set up disk usage monitoring and alerts',
                'Regular cleanup cron jobs',
            ],
        },
        # Permission Errors
        r'permission\s+denied': {
            'summary': 'Permission Denied Error',
            'root_cause': 'The process does not have sufficient permissions to access the requested resource.',
            'confidence': DiagnosisConfidence.VERY_HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check File Permissions',
                    description='Review file and directory permissions.',
                    command='ls -la <path>',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Process User',
                    description='Verify which user the process is running as.',
                    command='ps aux | grep <process>',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Use appropriate service accounts',
                'Implement principle of least privilege',
                'Set proper umask values',
            ],
        },
        # Database Errors
        r'(?:database|sql|deadlock)\s+error': {
            'summary': 'Database Error',
            'root_cause': 'An error occurred while executing a database operation or resolving transaction lock.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Check Database Status',
                    description='Verify the database service is running.',
                    command='systemctl status mysql || systemctl status postgresql',
                    severity='high',
                ),
                RecommendedFix(
                    title='Review Query & Deadlocks',
                    description='Check SQL queries for deadlock conditions and missing indexes.',
                    severity='medium',
                ),
                RecommendedFix(
                    title='Check Connection Pool',
                    description='Verify connection pool is not exhausted.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Use connection pooling with timeouts',
                'Implement idempotent transaction retries',
                'Add database performance monitoring',
            ],
        },
        # Authentication Errors
        r'authentication\s+failed|invalid\s+credentials': {
            'summary': 'Authentication Failure',
            'root_cause': 'The provided credentials were invalid or authentication failed.',
            'confidence': DiagnosisConfidence.HIGH,
            'recommendations': [
                RecommendedFix(
                    title='Verify Credentials',
                    description='Check that usernames, tokens, and passwords are valid.',
                    severity='high',
                ),
                RecommendedFix(
                    title='Check Authentication Service',
                    description='Verify LDAP, OAuth, or authentication provider connectivity.',
                    severity='medium',
                ),
            ],
            'prevention_tips': [
                'Implement credential rotation',
                'Use secure secret management services',
                'Set up authentication audit logs',
            ],
        },
    }

    def __init__(self) -> None:
        self._compiled_patterns = {
            pattern: re.compile(pattern, re.IGNORECASE)
            for pattern in self.KNOWLEDGE_BASE.keys()
        }

    def find_matching_diagnosis(
        self,
        error_message: str,
        context: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Find the best matching diagnosis for an error message."""
        context_str = context if context else ""
        combined_text = f"{error_message} {context_str}".strip()

        best_match = None
        best_score = 0

        for pattern, compiled in self._compiled_patterns.items():
            match = compiled.search(combined_text)
            if match:
                score = len(match.group())
                if score > best_score:
                    best_score = score
                    best_match = pattern

        if best_match:
            return best_match, self.KNOWLEDGE_BASE[best_match]

        return None


class AIDiagnosticEngine:
    """
    AI-powered diagnostic engine for log analysis.
    Cascades gracefully: OpenAI API -> Local llama-server -> Local Ollama -> Local Knowledge Base.
    """

    SYSTEM_PROMPT = (
        "You are an expert DevOps engineer. Analyze the provided log line. "
        "Return a concise JSON object with exact fields: 'summary', 'confidence' (HIGH/MEDIUM/LOW), "
        "'root_cause', and 'recommendations' (a list of objects with 'title' and 'description', or a list of strings). "
        "Do not wrap output in markdown codeblocks."
    )

    def __init__(self, timeout_seconds: float = 2.5) -> None:
        self.knowledge_base = ErrorKnowledgeBase()
        self._diagnosis_history: list[Diagnosis] = []
        self.timeout_seconds = timeout_seconds

    def _query_ai(self, error_message: str, context: str | None = None) -> Diagnosis | None:
        """
        Dynamically query OpenAI or local LLM instances (llama-server / Ollama) with timeout.
        Returns a Diagnosis object on success or None to trigger rule-based fallback.
        """
        openai_key = os.getenv("OPENAI_API_KEY")

        endpoints: list[tuple[str, dict[str, str], str]] = []

        if openai_key:
            endpoints.append((
                "https://api.openai.com/v1/chat/completions",
                {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {openai_key}",
                },
                os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            ))

        # Always add local fallbacks
        endpoints.extend([
            (
                "http://localhost:8080/v1/chat/completions",
                {"Content-Type": "application/json"},
                "local-model",
            ),
            (
                "http://localhost:11434/v1/chat/completions",
                {"Content-Type": "application/json"},
                "llama3",
            ),
        ])

        user_content = f"Log Line: {error_message}"
        if context:
            user_content += f"\nContext Logs:\n{context}"

        for url, headers, model in endpoints:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "max_tokens": 400,
            }

            try:
                data_bytes = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                    if response.status == 200:
                        raw_body = response.read().decode("utf-8")
                        res_json = json.loads(raw_body)
                        choices = res_json.get("choices", [])
                        if not choices:
                            continue

                        content_str = choices[0]["message"]["content"].strip()

                        # Clean any unexpected markdown wrapping
                        cleaned = re.sub(r"^```(?:json)?\s*", "", content_str, flags=re.IGNORECASE)
                        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

                        parsed = json.loads(cleaned)
                        if isinstance(parsed, dict):
                            return self._build_diagnosis_from_ai_json(parsed)
            except Exception:
                # Move onto the next available LLM endpoint
                continue

        return None

    def _build_diagnosis_from_ai_json(self, data: dict[str, Any]) -> Diagnosis:
        """Convert AI JSON response into a structured Diagnosis object."""
        summary = str(data.get("summary", "AI Log Diagnostics"))
        root_cause = str(data.get("root_cause", "No specific root cause identified."))

        # Map confidence safely
        conf_str = str(data.get("confidence", "MEDIUM")).upper()
        confidence = DiagnosisConfidence.MEDIUM
        if "VERY_HIGH" in conf_str or "VERY HIGH" in conf_str:
            confidence = DiagnosisConfidence.VERY_HIGH
        elif "HIGH" in conf_str:
            confidence = DiagnosisConfidence.HIGH
        elif "LOW" in conf_str:
            confidence = DiagnosisConfidence.LOW

        # Map recommendations list
        raw_recs = data.get("recommendations", [])
        recommendations: list[RecommendedFix] = []
        if isinstance(raw_recs, list):
            for i, rec in enumerate(raw_recs, 1):
                if isinstance(rec, str):
                    recommendations.append(
                        RecommendedFix(
                            title=f"Step {i}",
                            description=rec.strip(),
                        )
                    )
                elif isinstance(rec, dict):
                    recommendations.append(
                        RecommendedFix(
                            title=rec.get("title", f"Step {i}"),
                            description=rec.get("description", str(rec)),
                            command=rec.get("command"),
                        )
                    )

        return Diagnosis(
            error_summary=summary,
            root_cause=root_cause,
            confidence=confidence,
            recommendations=recommendations,
            prevention_tips=[
                "Review telemetry metrics during similar failures",
                "Ensure automated health check alerting is enabled",
            ],
        )

    def diagnose(self, error_message: str, context: str | None = None) -> Diagnosis:
        """
        Generate a diagnosis for an error message.
        Tries AI services first; gracefully falls back to local knowledge base.
        """
        # 1. Attempt dynamic AI diagnosis (OpenAI -> llama-server -> Ollama)
        ai_diagnosis = self._query_ai(error_message, context)
        if ai_diagnosis:
            self._diagnosis_history.append(ai_diagnosis)
            return ai_diagnosis

        # 2. Fallback: Find matching diagnosis from local knowledge base
        match = self.knowledge_base.find_matching_diagnosis(error_message, context)
        if match:
            _, diagnosis_data = match
            diagnosis = Diagnosis(
                error_summary=diagnosis_data.get("summary", "Unknown Error"),
                root_cause=diagnosis_data.get("root_cause", "Unable to determine root cause"),
                confidence=diagnosis_data.get("confidence", DiagnosisConfidence.MEDIUM),
                recommendations=list(diagnosis_data.get("recommendations", [])),
                prevention_tips=list(diagnosis_data.get("prevention_tips", [])),
            )
        else:
            # 3. Fallback: Generic rule-based diagnosis for unmatched errors
            diagnosis = self._generate_generic_diagnosis(error_message)

        self._diagnosis_history.append(diagnosis)
        return diagnosis

    def _generate_generic_diagnosis(self, error_message: str) -> Diagnosis:
        """Generate a generic diagnosis when no specific match is found."""
        error_type = self._infer_error_type(error_message)

        return Diagnosis(
            error_summary=error_type,
            root_cause="Error pattern not recognized. Manual inspection recommended.",
            confidence=DiagnosisConfidence.LOW,
            recommendations=[
                RecommendedFix(
                    title="Check System Journal Logs",
                    description="Inspect recent system logs around the time of failure.",
                    command="journalctl -xe --no-pager -n 50",
                    severity="high",
                ),
                RecommendedFix(
                    title="Review Service Status",
                    description="Check running services and recent restarts.",
                    command="systemctl --failed",
                    severity="medium",
                ),
                RecommendedFix(
                    title="Check Application Configuration",
                    description="Review environment variables and configuration files.",
                    severity="low",
                ),
            ],
            prevention_tips=[
                "Implement structured logging (JSON)",
                "Set up real-time anomaly alerts",
            ],
        )

    def _infer_error_type(self, error_message: str) -> str:
        """Infer error type from message content."""
        error_lower = error_message.lower()

        type_indicators = [
            ("Network", ["connection", "socket", "tcp", "udp", "http", "url", "refused"]),
            ("Database", ["sql", "query", "database", "mysql", "postgres", "deadlock"]),
            ("Memory", ["memory", "heap", "stack", "allocation", "oom"]),
            ("Permission", ["permission", "access", "denied", "forbidden", "unauthorized"]),
            ("File/IO", ["file", "path", "directory", "disk", "io", "space"]),
        ]

        for error_type, indicators in type_indicators:
            if any(ind in error_lower for ind in indicators):
                return f"{error_type} Error"

        return "Unknown Error"

    def diagnose_from_logs(
        self,
        log_entries: list[Any],
        error_entry: Any,
    ) -> Diagnosis:
        """Diagnose an error using surrounding log context window."""
        error_msg = getattr(error_entry, "raw", getattr(error_entry, "message", str(error_entry)))

        # Find position of the error in log buffer to take surrounding context
        context_lines: list[str] = []
        try:
            idx = log_entries.index(error_entry)
            start_idx = max(0, idx - 5)
            end_idx = min(len(log_entries), idx + 6)
            window = log_entries[start_idx:end_idx]
        except (ValueError, IndexError):
            window = log_entries[-10:]

        for entry in window:
            msg = getattr(entry, "raw", getattr(entry, "message", str(entry)))
            context_lines.append(msg)

        context = "\n".join(context_lines) if context_lines else None
        return self.diagnose(error_msg, context)

    def get_diagnosis_summary(self, diagnosis: Diagnosis) -> str:
        """Get a formatted text summary of a diagnosis."""
        summary = f"[{diagnosis.confidence.value.upper()}] {diagnosis.error_summary}\n"
        summary += f"Root Cause: {diagnosis.root_cause}\n\n"
        summary += "Recommendations:\n"

        for i, rec in enumerate(diagnosis.recommendations, 1):
            summary += f"  {i}. {rec.title}\n"
            summary += f"     {rec.description}\n"
            if rec.command:
                summary += f"     Command: {rec.command}\n"
            summary += "\n"

        return summary

    def clear_history(self) -> None:
        """Clear diagnosis history."""
        self._diagnosis_history.clear()
