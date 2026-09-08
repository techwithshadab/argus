"""Minimal A2A client: one JSON-RPC `message/send` to an agent, returning its text.

Used by the worker to call the Watch agent directly for sweeps (the orchestrator is not
involved). Locally the URL is the compose service; on AWS it is the AgentCore invocation URL
for the watch runtime, signed with SigV4 by this task's IAM role."""

from __future__ import annotations

import json
import uuid
from typing import Any


def make_sigv4(region: str, service: str = "bedrock-agentcore"):
    """httpx.Auth that signs requests with this task's IAM credentials (AgentCore accepts SigV4)."""
    import httpx
    from botocore.auth import SigV4Auth as _SigV4
    from botocore.awsrequest import AWSRequest
    from botocore.session import get_session

    credentials = get_session().get_credentials()

    class _Auth(httpx.Auth):
        requires_request_body = True

        def auth_flow(self, request: httpx.Request):
            aws_req = AWSRequest(
                method=request.method,
                url=str(request.url),
                data=request.content,
                headers={
                    "Content-Type": request.headers.get(
                        "content-type", "application/json"
                    )
                },
            )
            _SigV4(credentials, service, region).add_auth(aws_req)
            request.headers.update(dict(aws_req.headers))
            yield request

    return _Auth()


def extract_text(obj: Any) -> str:
    """Concatenate every A2A text part in a message/task response, in document order."""
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("kind") == "text" and isinstance(x.get("text"), str):
                out.append(x["text"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return "\n".join(out)


def send_message(url: str, text: str, *, auth=None, timeout: float = 600) -> dict:
    """Returns {"text": ..., "raw": <json-rpc result>}. Raises httpx errors on transport failure and
    RuntimeError on a JSON-RPC error."""
    req = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    import httpx

    with httpx.Client(timeout=timeout, auth=auth) as c:
        r = c.post(url, json=req, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        body = r.json()
    if "error" in body:
        raise RuntimeError(f"A2A error: {json.dumps(body['error'])[:500]}")
    result = body.get("result", body)
    return {"text": extract_text(result), "raw": result}
