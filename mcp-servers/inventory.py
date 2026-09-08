"""Write tools.json: every server's tools with their input schemas, straight from the
FastMCP registrations. The CDK deploy publishes it to AWS Agent Registry and derives the
Cedar policies from it, so the catalog, the policies and the running servers agree.
Run inside the image (`make tools-inventory`): the servers import psycopg."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

SERVERS = ("ais", "registry", "geo", "imagery")


def inventory() -> dict:
    out = {
        "version": (Path(__file__).parent / "VERSION").read_text().strip(),
        "servers": {},
    }
    for name in SERVERS:
        mcp = importlib.import_module(f"servers.{name}").mcp
        tools = []
        for t in mcp._tool_manager.list_tools():
            tools.append(
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "inputSchema": t.parameters,
                }
            )
        out["servers"][name] = {
            "description": (mcp.instructions or "").strip() or f"Argus {name} tools",
            "tools": sorted(tools, key=lambda x: x["name"]),
        }
    return out


if __name__ == "__main__":
    target = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "tools.json"
    )
    target.write_text(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {target}")
