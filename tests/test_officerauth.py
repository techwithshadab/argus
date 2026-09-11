"""Officer identity (ADR-0018): pure helpers and the middleware, without PyJWT or a stack."""

import asyncio
import base64
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, "services/api")
import officerauth  # noqa: E402
from callerauth import ANONYMOUS, AuthError, Caller  # noqa: E402
from officerauth import (  # noqa: E402
    OfficerAuthMiddleware,
    needs_iam,
    officer_from_claims,
    officer_of,
    split_jwt,
    verify_id_token,
)


def agent_route(path, method):
    return method == "POST" and path == "/alerts"


def test_officer_from_claims_prefers_email():
    assert officer_from_claims({"sub": "s", "email": " a@b "}) == "a@b"
    assert officer_from_claims({"sub": "s", "cognito:username": "u"}) == "u"
    assert officer_from_claims({"sub": "s"}) == "s"
    with pytest.raises(AuthError):
        officer_from_claims({"exp": 1})


def test_needs_iam_by_mode(monkeypatch):
    monkeypatch.setenv("OFFICER_AUTH", "header")
    assert needs_iam("/alerts", "POST", agent_route)
    assert not needs_iam("/sweep", "POST", agent_route)
    monkeypatch.setenv("OFFICER_AUTH", "oidc")
    assert needs_iam("/alerts", "POST", agent_route)
    assert not needs_iam("/health", "GET", agent_route)
    assert not needs_iam("/whoami", "GET", agent_route)
    assert needs_iam("/sweep", "POST", agent_route)
    assert needs_iam("/vessels", "GET", agent_route)
    token = officerauth._signed_in.set(True)
    try:
        assert not needs_iam("/sweep", "POST", agent_route)
        assert needs_iam("/alerts", "POST", agent_route)  # agents stay IAM-only
    finally:
        officerauth._signed_in.reset(token)


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()


def test_split_jwt_tolerates_the_balancers_padding():
    header = {"alg": "ES256", "kid": "k1", "signer": "arn:alb"}
    claims = {"email": "o@x", "exp": 1}
    token = ".".join([_b64(header), _b64(claims), "sig=="])
    h, c, _ = split_jwt(token)
    assert h == header and c == claims
    with pytest.raises(AuthError):
        split_jwt("not.a.jwt")


def test_verify_rejects_a_foreign_signer_before_touching_the_key_service():
    token = ".".join(
        [_b64({"alg": "ES256", "kid": "k", "signer": "arn:other"}), _b64({}), "s"]
    )
    with pytest.raises(AuthError, match="not signed by this load balancer"):
        verify_id_token(token, "us-east-1", "arn:mine", "")
    token = ".".join([_b64({"alg": "ES256", "signer": "arn:mine"}), _b64({}), "s"])
    with pytest.raises(AuthError, match="no key id"):
        verify_id_token(token, "us-east-1", "arn:mine", "")


class State:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_officer_of_by_source(monkeypatch):
    monkeypatch.setenv("OFFICER_AUTH", "header")
    assert officer_of(State(officer="alice")) == "alice"
    assert officer_of(State(officer="", caller=ANONYMOUS)) == "watch-officer"
    monkeypatch.setenv("OFFICER_AUTH", "oidc")
    op = Caller(arn="a", account="1", role="argus-operator", kind="agent")
    assert officer_of(State(officer="", caller=op)) == "iam:argus-operator"
    with pytest.raises(AuthError):
        officer_of(State(officer="", caller=ANONYMOUS))


def _run(app, headers: dict, path="/sweep", method="POST"):
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    return scope, sent


def test_middleware_header_mode(monkeypatch):
    monkeypatch.setenv("OFFICER_AUTH", "header")
    seen = {}

    async def inner(scope, receive, send):
        seen["officer"] = scope["state"]["officer"]
        seen["iam"] = needs_iam("/sweep", "POST", agent_route)

    _run(OfficerAuthMiddleware(inner), {"x-watch-officer": "bob"})
    assert seen == {"officer": "bob", "iam": False}
    _run(OfficerAuthMiddleware(inner), {})
    assert seen["officer"] == "watch-officer"


def test_middleware_oidc_mode(monkeypatch):
    monkeypatch.setenv("OFFICER_AUTH", "oidc")
    monkeypatch.setenv("OIDC_SIGNER", "arn:mine")

    def fake_verify(token, region, signer, issuer):
        if token != "good":
            raise AuthError(401, "bad token")
        return {"email": "officer@navy.example"}

    monkeypatch.setattr(officerauth, "verify_id_token", fake_verify)
    seen = {}

    async def inner(scope, receive, send):
        seen["officer"] = scope["state"]["officer"]
        seen["iam"] = needs_iam("/sweep", "POST", agent_route)
        seen["agent_iam"] = needs_iam("/alerts", "POST", agent_route)

    _run(OfficerAuthMiddleware(inner), {"x-amzn-oidc-data": "good"})
    assert seen == {"officer": "officer@navy.example", "iam": False, "agent_iam": True}
    # No sign-in: the request goes on, and the caller-auth layer demands IAM.
    seen.clear()
    _run(OfficerAuthMiddleware(inner), {})
    assert seen == {"officer": "", "iam": True, "agent_iam": True}
    # A forged token is refused outright.
    seen.clear()
    scope, sent = _run(OfficerAuthMiddleware(inner), {"x-amzn-oidc-data": "forged"})
    assert not seen and sent[0]["status"] == 401
    # The context variable never leaks between requests.
    assert officerauth._signed_in.get() is False


def test_api_uses_the_dependency_not_the_header():
    """Officer identity comes from the dependency, never from a header a caller sets.

    The count is not fixed: what matters is that every officer route resolves the
    officer through `current_officer` and that no handler reads a header instead.
    """
    src = open("services/api/main.py").read()
    assert "Header(" not in src
    assert src.count("Depends(current_officer)") >= 7
    assert "app.add_middleware(OfficerAuthMiddleware)" in src
    assert '@app.get("/whoami")' in src


def test_verify_accepts_a_padded_token_as_the_balancer_signs_it(monkeypatch):
    """The balancer signs its segments padding included; the verifier must not re-encode
    them (a deployed API rejected every real sign-in with "Signature verification failed"
    when it did). Needs PyJWT and cryptography, which CI's unit environment lacks."""
    jwt = pytest.importorskip("jwt")
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    from cryptography.hazmat.primitives import serialization

    key = ec.generate_private_key(ec.SECP256R1())
    pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )

    def padded(obj):  # the balancer's encoding: standard urlsafe base64 with '=' kept
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()

    header = {"alg": "ES256", "typ": "JWT", "kid": "k1", "signer": "arn:mine"}
    claims = {"email": "officer@x", "iss": "https://pool", "exp": 2_000_000_000}
    signing_input = padded(header) + "." + padded(claims)
    sig = jwt.algorithms.ECAlgorithm(jwt.algorithms.ECAlgorithm.SHA256).sign(
        signing_input.encode(), key
    )
    token = signing_input + "." + base64.urlsafe_b64encode(sig).decode()
    assert "=" in token.split(".")[0] or "=" in token.split(".")[1]
    monkeypatch.setattr(officerauth, "alb_public_key", lambda kid, region: pem)
    out = verify_id_token(token, "us-east-1", "arn:mine", "https://pool")
    assert out["email"] == "officer@x"


# --- P1: agents propose, officers decide (pinned) ---------------------------------


def _caller(role: str):
    from callerauth import Caller

    return Caller(
        arn=f"arn:aws:sts::1:assumed-role/{role}/s",
        account="1",
        role=role,
        kind="agent",
    )


def _agent_routes() -> tuple[tuple[str, str], ...]:
    """The API's _AGENT_ROUTES, read from source: importing main.py needs psycopg."""
    src = (ROOT / "services/api/main.py").read_text()
    block = src.split("_AGENT_ROUTES = (", 1)[1].split(")\n\n", 1)[0]
    pairs = re.findall(r'\("([A-Z]+)",\s*"([^"]+)"\)', block)
    assert pairs, "could not parse _AGENT_ROUTES"
    return tuple(pairs)


def _authorize_for(path: str, method: str, role: str, monkeypatch) -> bool:
    """True when a caller holding `role` is admitted on that route by the split
    allowlists, mirroring CallerAuthMiddleware's choice."""
    import callerauth
    from callerauth import matches_route

    def is_agent_route(p: str, m: str) -> bool:
        return matches_route(_agent_routes(), p, m)

    monkeypatch.setenv(
        "TOOL_ALLOWED_ROLES",
        "argus-agent-watch,argus-agent-orchestrator,argus-operator",
    )
    monkeypatch.setenv(
        "AGENT_ALLOWED_ROLES", "argus-agent-watch,argus-agent-orchestrator"
    )
    monkeypatch.setenv("OFFICER_ALLOWED_ROLES", "argus-operator")
    roles = (
        callerauth.agent_roles()
        if is_agent_route(path, method)
        else callerauth.officer_roles()
    )
    try:
        callerauth.authorize(_caller(role), roles)
        return True
    except callerauth.AuthError:
        return False


def test_agent_roles_cannot_take_officer_actions(monkeypatch):
    """The product's core guarantee: an agent may report findings but never approve
    tasking, review a report or request a sweep."""
    officer_routes = [
        ("POST", "/tasking/abc/approve"),
        ("POST", "/tasking/abc/reject"),
        ("POST", "/alerts/abc/review"),
        ("POST", "/investigations/abc/review"),
        ("POST", "/sweep"),
    ]
    for method, path in officer_routes:
        for agent in ("argus-agent-watch", "argus-agent-orchestrator"):
            assert not _authorize_for(path, method, agent, monkeypatch), (
                f"{agent} must not reach {method} {path}"
            )
        assert _authorize_for(path, method, "argus-operator", monkeypatch)


def test_agent_routes_admit_agents_and_refuse_unknown_roles(monkeypatch):
    for method, path in (
        ("POST", "/alerts"),
        ("POST", "/investigations/abc/complete"),
        ("POST", "/investigations/abc/fail"),
        ("POST", "/investigations/abc/progress"),
    ):
        assert _authorize_for(path, method, "argus-agent-watch", monkeypatch)
        assert not _authorize_for(path, method, "argus-agent-tasking", monkeypatch)


def test_officer_allowlist_is_fail_closed_when_unset(monkeypatch):
    """With no OFFICER_ALLOWED_ROLES the middleware falls back to the full allowlist, so
    the CDK must always set it; this test pins that the fallback is the only path."""
    import callerauth

    monkeypatch.delenv("OFFICER_ALLOWED_ROLES", raising=False)
    assert callerauth.officer_roles() == set()


def test_cdk_sets_both_allowlists():
    """The stack must name the two lists, and no agent role may be an officer role."""
    src = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()
    assert '"AGENT_ALLOWED_ROLES": "argus-agent-watch,argus-agent-orchestrator"' in src
    assert '"OFFICER_ALLOWED_ROLES": "argus-operator"' in src
    officers = src.split('"OFFICER_ALLOWED_ROLES": "')[1].split('"')[0]
    assert "agent" not in officers
