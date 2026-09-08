"""SigV4 signing for A2A calls between agents hosted on Bedrock AgentCore Runtime.

Locally (docker compose) A2A_AUTH=none and this is unused. On AWS the orchestrator calls the
specialist runtimes through the AgentCore invocation endpoint, which accepts SigV4 (IAM) or a
JWT from the configured authorizer. IAM keeps the demo free of an OAuth dance."""

from __future__ import annotations

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session


class AgentCoreSigV4(httpx.Auth):
    requires_request_body = True

    def __init__(self, region: str):
        self.region = region
        self.credentials = get_session().get_credentials()

    def auth_flow(self, request: httpx.Request):
        aws_req = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={
                "Content-Type": request.headers.get("content-type", "application/json")
            },
        )
        SigV4Auth(self.credentials, "bedrock-agentcore", self.region).add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


def httpx_client_args(a2a_auth: str, region: str) -> dict:
    if a2a_auth == "sigv4":
        return {"auth": AgentCoreSigV4(region), "timeout": 300}
    return {"timeout": 300}
