"""Entry point: mounts the selected FastMCP server as a Starlette app (streamable HTTP),
adds a /health route for load balancers and instruments it with OpenTelemetry."""

import importlib
import os

import uvicorn
from common.callerauth import CallerAuthMiddleware
from common.identity import WorkloadTokenMiddleware
from common.telemetry import SERVER_VERSION, configure
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

name = os.environ.get("MCP_SERVER", "ais")
port = int(os.environ.get("MCP_PORT", "8000"))
configure(f"mcp-{name}")

module = importlib.import_module(f"servers.{name}")
mcp = module.mcp
mcp.settings.host = "0.0.0.0"
mcp.settings.port = port
# FastMCP turns on DNS-rebinding protection (Host must be localhost) when it is constructed
# with its default host, and changing the host afterwards does not turn it off. Behind the
# internal ALB the Host header is the ALB name, so every request would get 421. Caller
# identity is checked by CallerAuthMiddleware instead.
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)
app = mcp.streamable_http_app()


async def health(_: Request):
    return JSONResponse(
        {
            "status": "ok",
            "server": name,
            "version": SERVER_VERSION,
            "tools": [t.name for t in mcp._tool_manager.list_tools()],
        }
    )


app.router.routes.insert(0, Route("/health", health))
# Verified caller identity on every MCP call (TOOL_AUTH=aws-iam); /health stays open for load balancers.
app.add_middleware(
    CallerAuthMiddleware, protected=lambda path, method: path.startswith("/mcp")
)
app.add_middleware(WorkloadTokenMiddleware)

try:
    from opentelemetry.instrumentation.starlette import StarletteInstrumentor

    StarletteInstrumentor().instrument_app(app)
except Exception:  # noqa: BLE001
    pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
