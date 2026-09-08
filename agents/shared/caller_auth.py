"""Caller identity for calls from an agent to the MCP servers and the platform API.

With TOOL_AUTH=aws-iam every request carries `Authorization: Bearer <token>` where the token
is a SigV4-signed sts:GetCallerIdentity request made with this agent's own IAM role. The
server forwards it to STS to learn who we are (see mcp-servers/common/callerauth.py). Tokens
are re-minted well inside STS's 15-minute window, so long-lived clients keep working.
With TOOL_AUTH=none (local compose) no header is added."""

from __future__ import annotations

import base64
import json
import threading
import time

import httpx

from .config import settings

STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
TOKEN_TTL_S = 10 * 60


class CallerIdentityAuth(httpx.Auth):
    requires_request_body = False

    def __init__(self, region: str):
        self.region = region
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and self._expires > time.time():
                return self._token
            self._token = mint_token(self.region)
            self._expires = time.time() + TOKEN_TTL_S
            return self._token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.token()}"
        yield request


def mint_token(region: str) -> str:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import get_session

    creds = get_session().get_credentials()
    if creds is None:
        raise RuntimeError("TOOL_AUTH=aws-iam but no AWS credentials are available")
    req = AWSRequest(
        method="POST",
        url=f"https://sts.{region}.amazonaws.com/",
        data=STS_BODY,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    SigV4Auth(creds.get_frozen_credentials(), "sts", region).add_auth(req)
    payload = {
        "v": 1,
        "method": "POST",
        "url": req.url,
        "headers": {k: v for k, v in req.headers.items()},
        "body": STS_BODY,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


_auth: CallerIdentityAuth | None = None


def tool_auth() -> httpx.Auth | None:
    """The httpx Auth to attach to MCP and API clients, or None when auth is off."""
    global _auth
    if settings.tool_auth != "aws-iam":
        return None
    if _auth is None:
        _auth = CallerIdentityAuth(settings.aws_region)
    return _auth
