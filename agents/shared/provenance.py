"""Provenance manifest (phase 4, ADR-0006): everything a report was produced with.

Prompts are hashed from the files in the image, the code revision comes from GIT_SHA baked at
build time, models are recorded per node by the graph, the guardrail id and version come from
the runtime's environment, and MCP server versions are read from each server's /health. The
API attaches the evidence snapshot ids when it stores the report."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

from .prompts_loader import PROMPTS

SCHEMA_VERSION = "1.1"  # bump when any contract in schemas.py changes shape


def prompt_hashes() -> dict[str, str]:
    out = {}
    for p in sorted(PROMPTS.glob("*.md")):
        out[p.stem] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


def code_revision() -> str:
    return os.getenv("GIT_SHA", "unknown")


def guardrail() -> dict:
    """Which Bedrock Guardrail guarded this run, read from the runtime's own environment.

    Recorded per run rather than looked up later, because `CfnGuardrailVersion` is a snapshot:
    a policy edit cuts a new version, so asking Bedrock tomorrow answers a different question
    than "what guarded this report". An auditor needs the second.

    `{}` when no guardrail is configured (local compose), so the field reads as "not recorded"
    instead of inventing a default that would be indistinguishable from a real one.
    """
    gid = os.getenv("BEDROCK_GUARDRAIL_ID", "")
    if not gid:
        return {}
    return {
        "id": gid,
        # DRAFT only ever appears locally: on AWS the runtimes get a numbered version through
        # Fn::GetAtt [GuardrailVersion, Version] (see docs/trustmodel/DEPLOYMENT.md).
        "version": os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT"),
    }


def mcp_versions(urls: dict[str, str], timeout: float = 3) -> dict[str, str]:
    """{server: version} from each MCP server's /health; 'unreachable' when it does not answer."""
    import httpx

    out = {}
    for name, url in urls.items():
        try:
            r = httpx.get(url.replace("/mcp", "/health"), timeout=timeout)
            out[name] = str(r.json().get("version", "unversioned"))
        except Exception:  # noqa: BLE001
            out[name] = "unreachable"
    return out


def manifest(nodes: list[dict], mcp: dict[str, str], extra: dict | None = None) -> dict:
    """nodes: [{node, agent, provider, model_id, tier, attempts, prompt}] as recorded by the graph."""
    return {
        "manifest_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "code_revision": code_revision(),
        "schema_version": SCHEMA_VERSION,
        "prompts": prompt_hashes(),
        "guardrail": guardrail(),
        "nodes": nodes,
        "mcp_servers": mcp,
        **(extra or {}),
    }
