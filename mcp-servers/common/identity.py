"""AgentCore Identity on a tool runtime. AgentCore Runtime injects the caller's workload
access token into every invocation (`X-Amz-Bedrock-AgentCore-Identity-WAT`, older
`WorkloadAccessToken`); this middleware keeps it for the duration of the request so a tool
can exchange it for a credential in the token vault (`GetResourceApiKey`)."""

from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

WAT_HEADERS = (b"x-amz-bedrock-agentcore-identity-wat", b"workloadaccesstoken")
_token: ContextVar[str | None] = ContextVar("workload_access_token", default=None)


def workload_access_token() -> str | None:
    return _token.get()


class WorkloadTokenMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            for h in WAT_HEADERS:
                if headers.get(h):
                    _token.set(headers[h].decode())
                    break
        await self.app(scope, receive, send)
