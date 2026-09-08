"""AgentCore runtime session ids, dependency-free so unit tests can pin the rule."""

from __future__ import annotations


def runtime_session_id(investigation_id: str, role: str = "orchestrator") -> str:
    """`rt-<investigation id>-<role>`, the same rule as `agents/shared/graph.py`: the collector
    rewrites the session id the runtime stamps on spans back to the investigation id, so
    every trace store shows one session per investigation. Must be at least 33 characters."""
    base = f"rt-{investigation_id or 'adhoc'}-{role}"
    return base if len(base) >= 33 else base + "-" * (33 - len(base))
