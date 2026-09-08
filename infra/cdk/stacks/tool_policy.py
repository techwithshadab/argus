"""Cedar policies for the AgentCore Gateway, derived from the tool inventory. Pure and
unit-tested: no CDK import.

Who may call what is one table, enforced outside
agent code by the gateway's policy engine (default deny, forbid wins):

* watch        -> ais, geo
* investigator -> ais, registry, geo (registry holds the encrypted personal data; only
                  the Investigator decrypts)
* tasking      -> imagery, geo (proposals only; approval is the API's human gate)
* orchestrator -> nothing (a code graph; its only model call has no tools)"""

from __future__ import annotations

import json
from pathlib import Path

ROLE_SERVERS: dict[str, tuple[str, ...]] = {
    "watch": ("ais", "geo"),
    "investigator": ("ais", "registry", "geo"),
    "tasking": ("imagery", "geo"),
}
GATEWAY_SEP = "___"


def load_inventory(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def tool_ids(inventory: dict, server: str) -> list[str]:
    """Gateway tool ids for one server: `<target>___<tool>`."""
    return [
        f"{server}{GATEWAY_SEP}{t['name']}"
        for t in inventory["servers"][server]["tools"]
    ]


def cedar_permit(role_arn: str, gateway_arn: str, actions: list[str]) -> str:
    acts = ",\n    ".join(f'AgentCore::Action::"{a}"' for a in actions)
    return (
        "permit(\n"
        f'  principal == AgentCore::IamEntity::"{role_arn}",\n'
        f"  action in [\n    {acts}\n  ],\n"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ");"
    )


def policies(
    inventory: dict,
    account: str,
    gateway_arn: str,
    role_name: str = "argus-agent-{role}",
) -> dict[str, str]:
    """One Cedar policy per agent role. The principal is the role's assumed-role identity,
    which is how the gateway sees an agent that signs with its runtime execution role."""
    out: dict[str, str] = {}
    for role, servers in ROLE_SERVERS.items():
        actions = [a for s in servers for a in tool_ids(inventory, s)]
        arn = f"arn:aws:sts::{account}:assumed-role/{role_name.format(role=role)}"
        out[role] = cedar_permit(arn, gateway_arn, actions)
    return out


def registry_tools_descriptor(inventory: dict, server: str) -> dict:
    """The MCP `tools` descriptor (protocol schema 2025-11-25) for a registry record."""
    return {
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in inventory["servers"][server]["tools"]
        ]
    }


MCP_SERVER_SCHEMA = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)
MCP_DESCRIPTION_MAX = 100  # server.schema.json: description maxLength


def short(text: str, limit: int = MCP_DESCRIPTION_MAX) -> str:
    """Cut at a word boundary under `limit` characters (the registry validates the
    MCP schema strictly: a 101-character description fails CreateRegistryRecord)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"


def registry_server_descriptor(inventory: dict, server: str, url: str) -> dict:
    """The MCP server.json (schema 2025-12-11) for a registry record."""
    return {
        "$schema": MCP_SERVER_SCHEMA,
        "name": f"io.argus/{server}",
        "description": short(inventory["servers"][server]["description"]),
        "version": inventory["version"],
        "remotes": [{"type": "streamable-http", "url": url}],
    }
