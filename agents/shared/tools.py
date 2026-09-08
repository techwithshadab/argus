"""Where the agents' tools come from.

Two modes, chosen by `TOOL_GATEWAY_URL`:

* **gateway** (AWS): every tool server sits behind one Amazon Bedrock AgentCore Gateway.
  Agents open a single MCP session to it, signed with their own IAM role (SigV4), and the
  gateway's policy engine decides per role which tools exist and may be called. Tool names
  carry the target name: `ais___find_ais_gaps`.
* **direct** (docker compose): one MCP client per server, tools prefixed by Strands
  (`ais_find_ais_gaps`) or bare with LangChain's adapter (`find_ais_gaps`).

Everything that touches a tool name goes through `tool_name()` / `split_tool_name()` so
the rest of the code never knows which mode it runs in."""

from __future__ import annotations

from .config import settings

SERVERS = ("ais", "registry", "geo", "imagery")
GATEWAY_SEP = "___"


def gateway_mode() -> bool:
    return bool(settings.tool_gateway_url)


def tool_name(server: str, tool: str) -> str:
    """The name to call a server's tool by in the current mode (direct calls, not agents)."""
    return f"{server}{GATEWAY_SEP}{tool}" if gateway_mode() else tool


def split_tool_name(
    name: str, servers: tuple[str, ...] = SERVERS
) -> tuple[str | None, str]:
    """(server, tool) from any spelling: `ais___find_ais_gaps`, `ais_find_ais_gaps`,
    `ais.find_ais_gaps` or a bare `find_ais_gaps` (server unknown)."""
    if GATEWAY_SEP in name:
        server, _, tool = name.partition(GATEWAY_SEP)
        return (server if server in servers else None), tool
    if "." in name:
        server, _, tool = name.partition(".")
        return (server if server in servers else None), tool
    for s in servers:
        if name.startswith(s + "_"):
            return s, name[len(s) + 1 :]
    return None, name


def canonical_source(name: str, tool_servers: dict[str, str] | None = None) -> str:
    """`server.tool`, the form the report policy and the evals expect. A bare tool name is
    resolved through `tool_servers` (tool -> server) when known."""
    server, tool = split_tool_name(name)
    if server is None and tool_servers and tool in tool_servers:
        server = tool_servers[tool]
    return f"{server}.{tool}" if server else name


def direct_url(server: str) -> str:
    return {
        "ais": settings.mcp_ais_url,
        "registry": settings.mcp_registry_url,
        "geo": settings.mcp_geo_url,
        "imagery": settings.mcp_imagery_url,
    }[server]


def _gateway_auth():
    from .a2a_auth import AgentCoreSigV4

    return AgentCoreSigV4(settings.aws_region)


def strands_tool_clients(servers: list[str]) -> list:
    """MCP clients for a Strands agent. Gateway: one client for everything the policy
    engine lets this role see. Direct: one per server, prefixed with the server name."""
    from strands.tools.mcp import MCPClient

    from .caller_auth import tool_auth

    if gateway_mode():
        return [
            MCPClient(
                url=settings.tool_gateway_url,
                auth_provider=_gateway_auth(),
                startup_timeout=60,
            )
        ]
    headers = (
        {"Authorization": f"Bearer {settings.mcp_bearer_token}"}
        if settings.mcp_bearer_token
        else None
    )
    return [
        MCPClient(
            url=direct_url(s),
            headers=headers,
            auth_provider=tool_auth(),
            prefix=s,
            startup_timeout=60,
        )
        for s in servers
    ]


def langchain_connections(servers: list[str]) -> dict[str, dict]:
    """Connection map for LangChain's MultiServerMCPClient (same two modes)."""
    from .caller_auth import tool_auth

    if gateway_mode():
        return {
            "gateway": {
                "transport": "streamable_http",
                "url": settings.tool_gateway_url,
                "auth": _gateway_auth(),
            }
        }
    out = {}
    for s in servers:
        conn: dict = {"transport": "streamable_http", "url": direct_url(s)}
        if settings.mcp_bearer_token:
            conn["headers"] = {"Authorization": f"Bearer {settings.mcp_bearer_token}"}
        if tool_auth() is not None:
            conn["auth"] = tool_auth()
        out[s] = conn
    return out


def detector_client():
    """A short-lived MCP client for code that calls tools itself (the Watch pre-pass)."""
    from strands.tools.mcp import MCPClient

    from .caller_auth import tool_auth

    if gateway_mode():
        return MCPClient(
            url=settings.tool_gateway_url,
            auth_provider=_gateway_auth(),
            startup_timeout=60,
        )
    return MCPClient(
        url=direct_url("ais"), auth_provider=tool_auth(), startup_timeout=60
    )


def health_urls() -> list[str]:
    """What `wait_for_mcp` polls at start-up: the gateway (no health route: the MCP endpoint
    itself answers) or each direct server's /health."""
    from .config import health_url

    if gateway_mode():
        return []
    return [health_url(direct_url(s)) for s in SERVERS]
