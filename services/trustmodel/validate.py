"""Validate an exported trace before spending a credit on it.

An agent evaluation costs 1 credit ($100) and a new account has 5. Discovering a malformed
trace after the charge is the expensive failure mode, so every structural check that can be
made offline is made here first.

Pure: takes the parsed document, returns problems. No network, no SDK required.
"""

from __future__ import annotations

REQUIRED_SPAN_KEYS = ("trace_id", "span_id", "ts", "tool_calls", "metadata")
REQUIRED_DOC_KEYS = ("trace_id", "goal", "spans", "agent_framework", "metadata")


def validate(doc: dict) -> list[str]:
    """Structural problems, worst first. Empty means it is worth uploading."""
    problems: list[str] = []

    for key in REQUIRED_DOC_KEYS:
        if not doc.get(key):
            problems.append(f"document is missing {key!r}")

    spans = doc.get("spans") or []
    if not spans:
        problems.append("document has no spans; nothing to evaluate")
        return problems

    seen: set[str] = set()
    for i, span in enumerate(spans):
        for key in REQUIRED_SPAN_KEYS:
            if key not in span:
                problems.append(f"span {i} is missing {key!r}")
        sid = span.get("span_id")
        if sid in seen:
            problems.append(f"span {i} reuses span_id {sid!r}; ids must be unique")
        seen.add(sid)
        if span.get("trace_id") != doc.get("trace_id"):
            problems.append(f"span {i} trace_id does not match the document trace_id")
        if not span.get("ts"):
            problems.append(
                f"span {i} has no timestamp; ordering cannot be established"
            )

    if not any(s["metadata"].get("node") == "report" for s in spans):
        problems.append(
            "no report node in the trace; the graph did not produce a product"
        )

    if not any(s.get("tool_calls") for s in spans):
        problems.append("no tool calls in any span; trajectory scoring will be empty")

    meta = doc.get("metadata") or {}
    if meta.get("redacted") is None:
        problems.append("metadata does not state whether content was redacted")

    return problems


def summarise(doc: dict) -> dict:
    """Counts a reviewer would want before approving an upload."""
    spans = doc.get("spans") or []
    meta = doc.get("metadata") or {}
    return {
        "spans": len(spans),
        "tool_calls": sum(len(s.get("tool_calls") or []) for s in spans),
        "human_decisions": sum(
            1 for s in spans if (s.get("metadata") or {}).get("human_decision")
        ),
        "agent_nodes": sorted(
            {
                (s.get("metadata") or {}).get("node")
                for s in spans
                if (s.get("metadata") or {}).get("agent")
            }
        ),
        "models": sorted({s["model"] for s in spans if s.get("model")}),
        "redacted": meta.get("redacted"),
        "goal_achieved": doc.get("goal_achieved"),
        "evidence_count": meta.get("evidence_count"),
    }
