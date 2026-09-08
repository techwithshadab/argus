"""Direct A2A calls from the orchestrator graph to the specialist agents (phase 4).

One JSON-RPC `message/send` per call, signed with SigV4 on AWS. The graph is code, so it calls
the specialists itself instead of handing an LLM a tool that might or might not call them."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from .a2a_auth import httpx_client_args
from .config import settings
from .graph import json_object as _json_object


def extract_text(obj: Any) -> str:
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


def send(
    url: str, text: str, timeout: float = 600, session_id: str | None = None
) -> str:
    """Send one user message to an A2A agent and return the text it answered with.
    `session_id` becomes the AgentCore runtime session on AWS (ignored by the local
    containers): parallel calls need distinct sessions, see `runtime_session_id`."""
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
    args = httpx_client_args(settings.a2a_auth, settings.aws_region)
    args["timeout"] = timeout
    with httpx.Client(**args) as c:
        headers = {"Content-Type": "application/json"}
        if session_id:
            headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = session_id
        r = c.post(url, json=req, headers=headers)
        r.raise_for_status()
        body = r.json()
    if "error" in body:
        raise RuntimeError(f"A2A error from {url}: {json.dumps(body['error'])[:500]}")
    return extract_text(body.get("result", body))


def json_object(text: str) -> dict:
    """The first JSON object in an agent's answer, or a ValueError (see shared.graph)."""
    return _json_object(text)
