"""Argus investigation -> TrustModel canonical trace spans.

Pure: no DB, no network, no boto3. `export_trace.py` reads the rows and calls in here, so the
shaping logic stays unit-testable under the repo's dependency-free test rule (CLAUDE.md).

The canonical span shape is the one TrustModel's transformer layer publishes:

    {trace_id, span_id, ts, model, input_hash, output_hash, tokens, latency_ms,
     tool_calls, metadata}

Two Argus facts drive the mapping:

* The provenance manifest (`agents/shared/provenance.py`) already records, per graph node,
  the provider, model id, tier, prompt hash, prompt version and attempt count. That is the
  span's `model` and most of its `metadata`, with no agent-side instrumentation.
* Report evidence is cited as `server.tool` (`agents/shared/graph.py` prefixes bare LangChain
  tool names), so the dotted source doubles as the tool-call name for the trajectory.

Content is hashed, never sent raw: `registry.beneficial_owner` and person names exist only
encrypted (three `datakey.py` copies) and the API stays pseudonymous. Raw content upload is a
deliberate, separate decision -- see `redact=False` in `investigation_spans`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Order the orchestrator's graph actually runs in (ADR-0003), used to sequence spans when a
#: manifest node carries no timestamp of its own.
#:
#: These are the names the orchestrator really records: the branches are written as
#: `f"investigator_{scope}"` (agents/orchestrator/app.py:260), NOT the bare "identity" and
#: "behaviour" the graph talks about. Verified against production investigation 4d991f3c.
NODE_ORDER = (
    "investigator_identity",
    "investigator_behaviour",
    "join",
    "tasking",
    "report",
    "persist",
)


def sha256_of(value: Any) -> str:
    """`sha256:<hex>` over a stable JSON rendering, so the same content always hashes alike."""
    if value is None:
        return ""
    raw = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, default=str)
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def span_id_for(trace_id: str, node: str, index: int) -> str:
    """Deterministic 16-hex span id.

    AgentCore does not hand us per-node span ids through the manifest, and a random id would
    make the same investigation export differently on every run -- which would defeat
    re-running an audit against a stored trace.
    """
    seed = f"{trace_id}:{index}:{node}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


#: Which nodes actually call tools, and which servers each may cite. The report node is a
#: single tool-less model call over validated JSON (CLAUDE.md), and `join`/`persist` are pure
#: code, so attributing tool calls to them would inflate the trajectory with calls that never
#: happened -- the opposite of what an audit wants.
#: Keyed on the orchestrator's real node names (see NODE_ORDER). The bare "identity" and
#: "behaviour" aliases are kept because `findings.provenance` uses them, and a future rename
#: should not silently empty the trajectory the way the original mismatch did.
NODE_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "investigator_identity": ("registry.", "geo."),
    "investigator_behaviour": ("ais.", "geo."),
    "identity": ("registry.", "geo."),
    "behaviour": ("ais.", "geo."),
    "tasking": ("imagery.", "geo."),
}


def tool_calls_from_evidence(evidence: list[dict], node: str) -> list[dict]:
    """Evidence entries belonging to `node`, as TrustModel tool_calls.

    Evidence sources are dotted `server.tool`. The identity branch owns `registry.*`, the
    behaviour branch owns `ais.*`, Tasking owns `imagery.*`; `geo.*` is shared context, so it
    is attributed to whichever branch is asking rather than duplicated.

    A node that calls no tools gets an empty list, never the full evidence set.
    """
    if node not in NODE_TOOL_PREFIXES:
        return []
    prefixes = NODE_TOOL_PREFIXES[node]
    out: list[dict] = []
    for item in evidence or []:
        source = (item.get("source") or "").strip()
        if not source:
            continue
        if not source.startswith(prefixes):
            continue
        server, _, tool = source.partition(".")
        out.append(
            {
                "name": source,
                "server": server or None,
                "tool": tool or None,
                "output_hash": sha256_of(item.get("summary")),
                "reference": item.get("reference"),
            }
        )
    return out


def node_span(
    trace_id: str,
    node: dict,
    index: int,
    evidence: list[dict],
    started_at: str | None,
) -> dict:
    """One manifest node -> one canonical span."""
    name = str(node.get("node") or f"node{index}")
    return {
        "trace_id": trace_id,
        "span_id": span_id_for(trace_id, name, index),
        "ts": started_at,
        "model": node.get("model_id"),
        "input_hash": "",
        "output_hash": "",
        "tokens": {},
        "latency_ms": None,
        "tool_calls": tool_calls_from_evidence(evidence, name),
        "metadata": {
            "node": name,
            "agent": node.get("agent"),
            "provider": node.get("provider"),
            "tier": node.get("tier"),
            "attempts": node.get("attempts", 1),
            "prompt_hash": node.get("prompt"),
            "prompt_version": node.get("prompt_version"),
        },
    }


def sort_key(node: dict, index: int) -> tuple[int, int]:
    name = str(node.get("node") or "")
    rank = NODE_ORDER.index(name) if name in NODE_ORDER else len(NODE_ORDER)
    return (rank, index)


def investigation_spans(
    investigation: dict,
    audit: list[dict] | None = None,
    redact: bool = True,
) -> list[dict]:
    """Canonical spans for one investigation.

    `investigation` is the DB row: id, mmsi, trace_id, status, trigger, report, manifest,
    created_at/updated_at. `audit` is the append-only trail for the same entity, which is what
    carries the human decisions -- those become spans too, so the evaluation can see that a
    person, not an agent, opened the investigation and approved (or did not approve) tasking.

    `redact=True` keeps content as hashes. Passing False embeds report text and must only be
    done for a deliberately non-personal subset.
    """
    trace_id = investigation.get("trace_id") or str(investigation.get("id") or "")
    manifest = investigation.get("manifest") or {}
    report = investigation.get("report") or {}
    evidence = report.get("evidence") or []
    created = _iso(investigation.get("created_at"))

    nodes = list(manifest.get("nodes") or [])
    ordered = sorted(enumerate(nodes), key=lambda p: sort_key(p[1], p[0]))

    spans = [node_span(trace_id, n, i, evidence, created) for i, n in ordered]

    if not redact:
        for span in spans:
            if span["metadata"]["node"] == "report":
                span["metadata"]["report_text"] = report.get("summary")

    spans.extend(_audit_spans(trace_id, audit or [], len(spans)))
    return spans


def _audit_spans(trace_id: str, audit: list[dict], offset: int) -> list[dict]:
    """Human and system decisions as spans, so accountability is visible in the trajectory."""
    out = []
    for i, row in enumerate(audit):
        action = str(row.get("action") or "")
        out.append(
            {
                "trace_id": trace_id,
                "span_id": span_id_for(trace_id, action, offset + i),
                "ts": _iso(row.get("ts")),
                "model": None,
                "input_hash": "",
                "output_hash": sha256_of(row.get("details")),
                "tokens": {},
                "latency_ms": None,
                "tool_calls": [],
                "metadata": {
                    "node": action,
                    "actor": row.get("actor"),
                    "actor_kind": row.get("actor_kind"),
                    "entity_kind": row.get("entity_kind"),
                    "entity_id": row.get("entity_id"),
                    "human_decision": row.get("actor_kind") == "watch_officer",
                },
            }
        )
    return out


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def trace_document(
    investigation: dict,
    audit: list[dict] | None = None,
    redact: bool = True,
) -> dict:
    """The full `trace.json` payload.

    The envelope TrustModel's `agentic.evaluate(file_path=...)` expects is not published in the
    wiki, so this carries the canonical spans under both `spans` and `trace` and states the
    goal alongside them. If their loader rejects it, the fallback is a custom transformer
    (`transformer.py`), which their docs size at about fifty lines.
    """
    report = investigation.get("report") or {}
    manifest = investigation.get("manifest") or {}
    spans = investigation_spans(investigation, audit, redact)
    goal_achieved = investigation.get("status") == "complete" and bool(report)

    return {
        "schema": "trustmodel.canonical.v1",
        "source": "argus",
        "trace_id": investigation.get("trace_id") or str(investigation.get("id") or ""),
        "agent_id": "argus-orchestrator",
        "goal": (
            "Investigate a dark-vessel alert and produce a Vessel of Interest report "
            "whose every claim cites a tool, proposing collection for human approval."
        ),
        "goal_achieved": goal_achieved,
        "agent_framework": "langgraph",
        "agent_model": _primary_model(manifest),
        "spans": spans,
        "trace": spans,
        "metadata": {
            "investigation_id": str(investigation.get("id") or ""),
            "mmsi": investigation.get("mmsi"),
            "trigger": investigation.get("trigger"),
            "status": investigation.get("status"),
            "review_state": investigation.get("review_state"),
            "code_revision": manifest.get("code_revision"),
            "schema_version": manifest.get("schema_version"),
            "manifest_version": manifest.get("manifest_version"),
            "mcp_servers": manifest.get("mcp_servers") or {},
            "prompts": manifest.get("prompts") or {},
            "models": models_used(manifest),
            "guardrail": guardrail_attestation(manifest),
            "priority": report.get("priority"),
            "confidence": report.get("confidence"),
            "evidence_count": len(report.get("evidence") or []),
            "information_gaps": len(report.get("information_gaps") or []),
            "redacted": redact,
        },
    }


def guardrail_attestation(manifest: dict) -> dict:
    """Which Bedrock Guardrail was in force, from the manifest if the run recorded it.

    Deliberately reads the manifest rather than querying Bedrock: the trace must attest to what
    guarded *this* run, and a live lookup would report today's configuration instead. When the
    manifest carries nothing, `recorded` is False and a reviewer knows not to trust the field
    rather than being handed a plausible-looking default.

    Resolving the version is not obvious: `list-guardrails` only shows the DRAFT, while the
    runtimes consume a numbered version through `BEDROCK_GUARDRAIL_VERSION`
    (`Fn::GetAtt [GuardrailVersion, Version]`). See docs/trustmodel/DEPLOYMENT.md.
    """
    guardrail = manifest.get("guardrail") or {}
    return {
        "recorded": bool(guardrail),
        "id": guardrail.get("id"),
        "version": guardrail.get("version"),
        "policy_hash": guardrail.get("policy_hash"),
    }


def models_used(manifest: dict) -> list[dict]:
    """Every distinct model this run actually used, with the tiers and nodes it served.

    Not cosmetic: tier escalation is live (`model_unavailable` in `shared/models.py` retries on
    the next tier up), so one investigation can legitimately run on two different Nova models.
    An auditor asking "which model produced this claim" needs the per-node answer, and a
    governance report needs the set. Production is Bedrock-only, Amazon Nova only (ADR-0002).
    """
    seen: dict[str, dict] = {}
    for node in manifest.get("nodes") or []:
        model_id = node.get("model_id")
        if not model_id:
            continue
        entry = seen.setdefault(
            model_id,
            {
                "model_id": model_id,
                "provider": node.get("provider"),
                "tiers": [],
                "nodes": [],
            },
        )
        tier = node.get("tier")
        if tier and tier not in entry["tiers"]:
            entry["tiers"].append(tier)
        name = node.get("node")
        if name and name not in entry["nodes"]:
            entry["nodes"].append(name)
    return [seen[k] for k in sorted(seen)]


def _primary_model(manifest: dict) -> str | None:
    """The report node's model: the one tool-less LLM call that writes the product."""
    for node in manifest.get("nodes") or []:
        if node.get("node") == "report":
            return node.get("model_id")
    nodes = manifest.get("nodes") or []
    return nodes[0].get("model_id") if nodes else None
