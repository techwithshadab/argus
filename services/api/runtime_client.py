"""Invokes the orchestrator either as a local container (docker compose) or on Bedrock AgentCore Runtime."""

from __future__ import annotations

import json
import os
import uuid

import httpx
from runtime_session import (
    runtime_session_id,  # noqa: F401  (re-exported for the worker)
)

MODE = os.getenv("ORCHESTRATOR_MODE", "local")  # local | agentcore
LOCAL_URL = os.getenv("ORCHESTRATOR_URL", "http://agent-orchestrator:8080")
REGION = os.getenv("AWS_REGION", "us-east-1")
_runtime_arn = os.getenv("ORCHESTRATOR_RUNTIME_ARN", "")


def runtime_arn() -> str:
    """The orchestrator runtime ARN comes from env or from SSM /argus/orchestrator-runtime-arn,
    which the agents stack writes. Reading it lazily avoids a circular CDK dependency."""
    global _runtime_arn
    if not _runtime_arn:
        import boto3

        _runtime_arn = boto3.client("ssm", region_name=REGION).get_parameter(
            Name=os.getenv(
                "ORCHESTRATOR_RUNTIME_ARN_PARAM", "/argus/orchestrator-runtime-arn"
            )
        )["Parameter"]["Value"]
    return _runtime_arn


def invoke(payload: dict, session_id: str | None = None) -> dict:
    if MODE == "agentcore":
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=REGION)
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn(),
            runtimeSessionId=session_id or runtime_session_id(str(uuid.uuid4())),
            payload=json.dumps(payload).encode(),
            contentType="application/json",
            accept="application/json",
        )
        body = (
            resp["response"].read()
            if hasattr(resp.get("response"), "read")
            else b"".join(resp["response"])
        )
        return json.loads(body)
    with httpx.Client(timeout=900) as c:
        r = c.post(f"{LOCAL_URL}/invocations", json=payload)
        r.raise_for_status()
        return r.json()
