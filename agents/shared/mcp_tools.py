"""Helpers to attach MCP servers to Strands agents over streamable HTTP."""

from __future__ import annotations

from strands.tools.mcp import MCPClient

from .caller_auth import tool_auth
from .config import health_url, settings


def mcp_client(url: str, prefix: str | None = None) -> MCPClient:
    headers = (
        {"Authorization": f"Bearer {settings.mcp_bearer_token}"}
        if settings.mcp_bearer_token
        else None
    )
    return MCPClient(
        url=url,
        headers=headers,
        auth_provider=tool_auth(),
        prefix=prefix,
        startup_timeout=60,
    )


def wait_for_mcp(urls: list[str], timeout: int = 180) -> None:
    """Block until every MCP server answers /health. Strands connects to MCP servers when the Agent is
    constructed, so this keeps agent start-up robust against ordering on compose and on AgentCore."""
    import time

    import httpx

    deadline = time.time() + timeout
    pending = {health_url(u) for u in urls}
    while pending and time.time() < deadline:
        for u in list(pending):
            try:
                if httpx.get(u, timeout=3).status_code == 200:
                    pending.discard(u)
            except Exception:  # noqa: BLE001
                pass
        if pending:
            time.sleep(2)
    if pending:
        raise RuntimeError(f"MCP servers not reachable: {sorted(pending)}")
