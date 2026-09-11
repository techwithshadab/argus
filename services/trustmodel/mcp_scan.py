"""Local MCP tool-surface scan for the four Argus servers.

TrustModel's Agent Governance product scans "every Model Context Protocol tool ... before an
agent registers it" for tool poisoning, prompt injection embedded in tool descriptions, and
over-permissive grants. That SKU is Q3 2026, so this runs the same three checks locally and
offline against `mcp-servers/tools.json`, the file that already generates the Cedar policies
and the Agent Registry records.

Pure and dependency-free: reads a dict, returns findings. `report.py` renders them and the
hosted scanner can replace or corroborate them later without changing the call site.

These are heuristics over tool metadata, not a proof of safety. A clean result means no
injection pattern was found in the text an agent is asked to trust -- nothing more.
"""

from __future__ import annotations

import re
from typing import Any

#: Phrases that try to steer a reading model rather than describe a tool. A tool description is
#: attacker-controlled text from the agent's point of view, which is why `common/safety.py`
#: wraps external free text in `untrusted()` in the first place.
INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore (?:all |any )?(?:previous|prior|above)", "instruction override"),
    (r"disregard (?:the |all )?(?:previous|prior|above)", "instruction override"),
    (r"system prompt", "system-prompt reference"),
    (r"you (?:are|must|should) (?:now|always)", "role reassignment"),
    (r"do not (?:tell|inform|mention)", "concealment instruction"),
    (r"<\s*(?:script|iframe)", "markup injection"),
    (r"</?\s*(?:system|assistant|user)\s*>", "chat-role markup"),
    (r"\bBEGIN\b.*\bINSTRUCTIONS?\b", "embedded instruction block"),
    (r"(?:api[_ ]?key|password|secret|token)\s*[:=]", "credential reference"),
)

#: Argument names that widen a tool's blast radius beyond reading data.
DANGEROUS_PARAMS: tuple[str, ...] = (
    "command",
    "cmd",
    "shell",
    "exec",
    "eval",
    "script",
    "sql",
    "query_raw",
    "path",
    "file",
    "url",
    "endpoint",
)

#: Verbs that imply a tool changes state. Argus's tool plane is read-only apart from the
#: tasking proposal, so anything else writing is worth a human look.
WRITE_VERBS: tuple[str, ...] = (
    "create",
    "delete",
    "update",
    "write",
    "set",
    "drop",
    "insert",
)

#: The one tool that legitimately writes, and only ever a `proposed` row for human approval.
EXPECTED_WRITERS: frozenset[str] = frozenset({"imagery.create_tasking_request"})

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def _finding(server: str, tool: str, check: str, severity: str, detail: str) -> dict:
    return {
        "server": server,
        "tool": f"{server}.{tool}" if tool else server,
        "check": check,
        "severity": severity,
        "detail": detail,
    }


def scan_description(server: str, tool: str, text: str) -> list[dict]:
    """Injection patterns in the description an agent is asked to read."""
    found = []
    low = (text or "").lower()
    for pattern, label in INJECTION_PATTERNS:
        if re.search(pattern, low, re.IGNORECASE | re.DOTALL):
            found.append(
                _finding(
                    server,
                    tool,
                    "prompt_injection",
                    "high",
                    f"description contains a {label} pattern (/{pattern}/)",
                )
            )
    return found


def scan_parameters(server: str, tool: str, schema: dict) -> list[dict]:
    """Parameters that grant more reach than a read-only maritime query needs."""
    found = []
    props = (schema or {}).get("properties") or {}
    for name in sorted(props):
        if name.lower() in DANGEROUS_PARAMS:
            found.append(
                _finding(
                    server,
                    tool,
                    "excessive_permission",
                    "medium",
                    f"parameter {name!r} can widen the tool's reach beyond a data read",
                )
            )
    return found


def scan_mutation(server: str, tool: str, description: str) -> list[dict]:
    """State-changing tools outside the one expected writer."""
    dotted = f"{server}.{tool}"
    if dotted in EXPECTED_WRITERS:
        return []
    verb = tool.split("_", 1)[0].lower()
    if verb in WRITE_VERBS:
        return [
            _finding(
                server,
                tool,
                "state_change",
                "medium",
                f"tool name implies a write ({verb!r}) but only {', '.join(sorted(EXPECTED_WRITERS))} "
                "is an expected writer",
            )
        ]
    return []


def scan_inventory(inventory: dict[str, Any]) -> dict:
    """Scan the whole `mcp-servers/tools.json` inventory.

    Returns {servers, tools, findings, by_severity, clean} so a governance report can state
    coverage as well as findings.
    """
    servers = inventory.get("servers") or {}
    findings: list[dict] = []
    tool_count = 0

    for server in sorted(servers):
        spec = servers[server] or {}
        for entry in spec.get("tools") or []:
            name = str(entry.get("name") or entry.get("title") or "")
            description = str(entry.get("description") or "")
            schema = entry.get("inputSchema") or {}
            tool_count += 1
            findings += scan_description(server, name, description)
            findings += scan_parameters(server, name, schema)
            if name:
                findings += scan_mutation(server, name, description)
        findings += scan_description(server, "", str(spec.get("description") or ""))

    findings.sort(
        key=lambda f: (-SEVERITY_RANK.get(f["severity"], 0), f["tool"], f["check"])
    )
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    return {
        "servers": sorted(servers),
        "server_count": len(servers),
        "tool_count": tool_count,
        "findings": findings,
        "by_severity": by_severity,
        "clean": not findings,
        "checks": ["prompt_injection", "excessive_permission", "state_change"],
        "inventory_version": inventory.get("version"),
    }
