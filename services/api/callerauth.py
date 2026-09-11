"""Verified caller identity for the MCP servers and the platform API.

Mode comes from TOOL_AUTH:
  none      accept every request (local compose); caller is "anonymous".
  aws-iam   require `Authorization: Bearer <token>` where the token is a SigV4-signed
            sts:GetCallerIdentity request minted by the caller with its own IAM role
            (see agents/shared/caller_auth.py). We forward that request to STS, which
            answers with the ARN of whoever signed it, and check the IAM role name against
            TOOL_ALLOWED_ROLES (comma-separated) and optionally TOOL_ALLOWED_ACCOUNT.

This is the technique HashiCorp Vault's AWS auth method and aws-iam-authenticator use:
the verifier holds no secret, STS is the only party that can validate the signature, and
one IAM role per agent (ADR-0001) is what makes the role name a meaningful identity.

This file is duplicated verbatim in mcp-servers/common/ and services/api/ so each image
stays self-contained; change both together.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("callerauth")

STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
_STS_HOST = re.compile(r"^sts(\.[a-z0-9-]+)?\.amazonaws\.com$")
_ALLOWED_HEADERS = {
    "authorization",
    "x-amz-date",
    "x-amz-security-token",
    "x-amz-content-sha256",
    "content-type",
    "host",
}
_ARN = re.compile(r"<Arn>([^<]+)</Arn>")
_ACCOUNT = re.compile(r"<Account>(\d+)</Account>")
TOKEN_MAX_AGE_S = 15 * 60  # STS rejects presigned requests older than this
CACHE_TTL_S = 5 * 60


@dataclass(frozen=True)
class Caller:
    arn: str
    account: str
    role: str  # IAM role name (or user name) that signed the request
    kind: str  # "agent" for verified IAM identities, "anonymous" when auth is off

    def as_dict(self) -> dict:
        return {
            "arn": self.arn,
            "account": self.account,
            "role": self.role,
            "kind": self.kind,
        }


ANONYMOUS = Caller(arn="", account="", role="anonymous", kind="anonymous")


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------- pure helpers (unit-tested without any SDK) ----------------
def encode_token(method: str, url: str, headers: dict, body: str) -> str:
    payload = {"v": 1, "method": method, "url": url, "headers": headers, "body": body}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> dict:
    """Parse and sanity-check a token. Raises AuthError(401) on anything unexpected."""
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as e:  # noqa: BLE001
        raise AuthError(401, "malformed caller token") from e
    if payload.get("v") != 1 or payload.get("method") != "POST":
        raise AuthError(401, "unsupported caller token")
    url = str(payload.get("url", ""))
    if not is_sts_url(url):
        raise AuthError(401, "caller token must target STS")
    if payload.get("body") != STS_BODY:
        raise AuthError(401, "caller token must be GetCallerIdentity")
    headers = {
        str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()
    }
    if not _ALLOWED_HEADERS.issuperset(headers) or "authorization" not in headers:
        raise AuthError(401, "caller token carries unexpected headers")
    if (
        "x-amz-date" not in headers
        or token_age_s(headers["x-amz-date"]) > TOKEN_MAX_AGE_S
    ):
        raise AuthError(401, "caller token expired")
    return {"url": url, "headers": headers, "body": STS_BODY}


def is_sts_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(_STS_HOST.match(parts.hostname or ""))


def token_age_s(amz_date: str, now: float | None = None) -> float:
    """Age of an X-Amz-Date (YYYYMMDDTHHMMSSZ) in seconds; huge when unparsable."""
    try:
        t = time.strptime(amz_date, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return float("inf")
    import calendar

    return (now if now is not None else time.time()) - calendar.timegm(t)


def role_name_from_arn(arn: str) -> str:
    """arn:aws:sts::123:assumed-role/Name/session -> Name; arn:aws:iam::123:role/Name -> Name;
    arn:aws:iam::123:user/Name -> Name. Empty when not recognised."""
    resource = arn.split(":", 5)[5] if arn.count(":") >= 5 else ""
    kind, _, rest = resource.partition("/")
    if kind in ("assumed-role", "role", "user") and rest:
        return rest.split("/")[0]
    return ""


def parse_sts_response(xml: str) -> tuple[str, str]:
    arn, account = _ARN.search(xml), _ACCOUNT.search(xml)
    if not arn or not account:
        raise AuthError(401, "STS did not confirm the caller")
    return arn.group(1), account.group(1)


def _roles(var: str) -> set[str]:
    return {r.strip() for r in os.getenv(var, "").split(",") if r.strip()}


def allowed_roles() -> set[str]:
    """Every role the service admits at all (agents and operators together)."""
    return _roles("TOOL_ALLOWED_ROLES")


def agent_roles() -> set[str]:
    """Roles allowed on agent-only routes. Defaults to the whole allowlist so the MCP
    servers, which have no officer routes, keep their single-list behaviour."""
    named = _roles("AGENT_ALLOWED_ROLES")
    return named or allowed_roles()


def officer_roles() -> set[str]:
    """Roles allowed to act as an officer (review, approve, sweep). An agent role must
    never appear here: agents propose, officers decide. Empty means no IAM caller may
    take an officer action, which is the safe default when the variable is unset."""
    return _roles("OFFICER_ALLOWED_ROLES")


def authorize(caller: Caller, roles: set[str], account: str = "") -> None:
    """Fail closed: in aws-iam mode an empty allowlist admits nobody."""
    if account and caller.account != account:
        raise AuthError(403, f"caller account {caller.account} is not allowed")
    if not caller.role or caller.role not in roles:
        raise AuthError(403, f"role {caller.role or '?'} is not allowed here")


def matches_route(
    patterns: tuple[tuple[str, str], ...], path: str, method: str
) -> bool:
    """(METHOD, "/a/*/b") patterns; "*" matches exactly one path segment."""
    parts = path.rstrip("/").split("/")
    for m, pattern in patterns:
        pp = pattern.split("/")
        if (
            m == method
            and len(pp) == len(parts)
            and all(a == "*" or a == b for a, b in zip(pp, parts, strict=True))
        ):
            return True
    return False


# ---------------- verification against STS ----------------
_cache: dict[str, tuple[Caller, float]] = {}


def verify_token(token: str) -> Caller:
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    req = decode_token(token)
    import httpx

    try:
        resp = httpx.post(
            req["url"], headers=req["headers"], content=req["body"], timeout=5
        )
    except httpx.HTTPError as e:
        raise AuthError(503, f"STS unreachable: {e}") from e
    if resp.status_code != 200:
        raise AuthError(401, "STS rejected the caller token")
    arn, account = parse_sts_response(resp.text)
    caller = Caller(
        arn=arn, account=account, role=role_name_from_arn(arn), kind="agent"
    )
    _cache[key] = (caller, time.time() + CACHE_TTL_S)
    if len(_cache) > 1000:
        _cache.clear()
    return caller


def mode() -> str:
    return os.getenv("TOOL_AUTH", "none").strip().lower()


def _every_route_is_an_agent_route(path: str, method: str) -> bool:
    return True


class CallerAuthMiddleware:
    """ASGI middleware: verifies the caller on protected paths and stores it as
    `request.state.caller` (a Caller). Unprotected paths get ANONYMOUS."""

    def __init__(
        self,
        app,
        protected: Callable[[str, str], bool],
        agent_route: Callable[[str, str], bool] | None = None,
    ):
        self.app = app
        self.protected = protected
        # Without an agent-route predicate every protected path is treated as one, which
        # is the MCP servers' case: they serve tools only.
        self.agent_route = agent_route or _every_route_is_an_agent_route

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        state["caller"] = ANONYMOUS
        if mode() == "aws-iam" and self.protected(scope["path"], scope["method"]):
            headers = {
                k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
            }
            auth = headers.get("authorization", "")
            try:
                if not auth.lower().startswith("bearer "):
                    raise AuthError(401, "caller token required")
                caller = verify_token(auth[7:].strip())
                # An agent route admits agent roles; anything else an IAM caller reaches
                # is an officer action, so it needs the officer allowlist (P1).
                roles = (
                    agent_roles()
                    if self.agent_route(scope["path"], scope["method"])
                    else officer_roles() or allowed_roles()
                )
                authorize(caller, roles, os.getenv("TOOL_ALLOWED_ACCOUNT", ""))
            except AuthError as e:
                log.warning(
                    "rejected %s %s: %s", scope["method"], scope["path"], e.message
                )
                return await _reject(send, e.status, e.message)
            state["caller"] = caller
        return await self.app(scope, receive, send)


async def _reject(send, status: int, message: str):
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
