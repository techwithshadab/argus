"""Who the watch officer is (ADR-0018).

OFFICER_AUTH=header   local compose: the X-Watch-Officer header, default "watch-officer";
                      nothing is verified.
OFFICER_AUTH=oidc     AWS: the public load balancer signs officers in with Cognito and
                      forwards the id token as `x-amzn-oidc-data`, a JWT signed by the
                      balancer's regional key (ES256). Every route that is neither public
                      nor agent-only must arrive with that token, or with a verified IAM
                      caller (callerauth.py: evals and operator tooling send a bearer
                      token, the balancer lets those through on /api/*). The officer id
                      is the email claim, or `iam:<role>` for an IAM caller. Agent routes
                      stay IAM-only in both modes.

The token's signature is checked against the key the balancer publishes for the `kid` in
the header, the `signer` header must be this deployment's balancer (OIDC_SIGNER) and the
issuer must be its user pool (OIDC_ISSUER). PyJWT is imported lazily so the pure helpers
stay testable without it."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from contextvars import ContextVar

from callerauth import AuthError, _reject, matches_route

log = logging.getLogger("officerauth")

DEFAULT_OFFICER = "watch-officer"
OIDC_HEADER = "x-amzn-oidc-data"
PUBLIC_ROUTES = (("GET", "/health"), ("GET", "/metrics"), ("GET", "/whoami"))
KEY_TTL_S = 6 * 60 * 60

# Set by OfficerAuthMiddleware for the request being served; read by the caller-auth
# predicate that decides whether an IAM token is required (see needs_iam).
_signed_in: ContextVar[bool] = ContextVar("officer_signed_in", default=False)


def mode() -> str:
    return os.getenv("OFFICER_AUTH", "header").strip().lower()


def is_public(path: str, method: str) -> bool:
    return matches_route(PUBLIC_ROUTES, path, method)


def officer_from_claims(claims: dict) -> str:
    """The id recorded on every decision: email, else the pool username, else the subject."""
    for key in ("email", "cognito:username", "username", "sub"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise AuthError(401, "no officer identity in the id token")


def needs_iam(path: str, method: str, agent_route) -> bool:
    """Predicate for CallerAuthMiddleware: agent routes always; in oidc mode every route
    that is not public and did not arrive with a verified sign-in."""
    if agent_route(path, method):
        return True
    if mode() != "oidc" or is_public(path, method):
        return False
    return not _signed_in.get()


def split_jwt(token: str) -> tuple[dict, dict, str]:
    """Header and claims of a JWT without verifying it. The balancer pads its segments with
    '=' (unlike RFC 7515), so padding is normalised here for parsing only."""

    def part(seg: str) -> bytes:
        seg = seg.rstrip("=")
        return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))

    try:
        h, c, s = token.split(".")
        return json.loads(part(h)), json.loads(part(c)), s
    except Exception as e:  # noqa: BLE001
        raise AuthError(401, "malformed id token") from e


_keys: dict[str, tuple[str, float]] = {}


def alb_public_key(kid: str, region: str) -> str:
    """PEM public key the balancer published for `kid` (cached; keys rotate rarely)."""
    hit = _keys.get(kid)
    if hit and hit[1] > time.time():
        return hit[0]
    if not kid.replace("-", "").isalnum():
        raise AuthError(401, "unexpected key id in the id token")
    import httpx

    url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
    try:
        resp = httpx.get(url, timeout=5)
    except httpx.HTTPError as e:
        raise AuthError(503, f"balancer key service unreachable: {e}") from e
    if resp.status_code != 200 or "BEGIN PUBLIC KEY" not in resp.text:
        raise AuthError(401, "balancer does not know the id token's key")
    _keys[kid] = (resp.text, time.time() + KEY_TTL_S)
    return resp.text


def verify_id_token(token: str, region: str, signer: str, issuer: str) -> dict:
    """Claims of a balancer-signed id token, or AuthError."""
    header, _, _ = split_jwt(token)
    if signer and header.get("signer") != signer:
        raise AuthError(401, "id token was not signed by this load balancer")
    kid = str(header.get("kid") or "")
    if not kid:
        raise AuthError(401, "id token has no key id")
    import jwt

    # The balancer signs the segments exactly as it sends them, padding included, so the
    # token goes to the verifier untouched: re-encoding the segments changes the signed
    # input and every token fails with "Signature verification failed".
    try:
        claims = jwt.decode(
            token,
            alb_public_key(kid, region),
            algorithms=["ES256"],
            issuer=issuer or None,
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as e:
        raise AuthError(401, f"id token rejected: {e}") from e
    return claims


class OfficerAuthMiddleware:
    """ASGI middleware, outermost: resolves `request.state.officer` and tells the caller-auth
    layer whether the request is already signed in."""

    def __init__(self, app):
        self.app = app
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.signer = os.getenv("OIDC_SIGNER", "")
        self.issuer = os.getenv("OIDC_ISSUER", "")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        state["officer"] = ""
        token = _signed_in.set(False)
        try:
            if mode() == "oidc":
                raw = headers.get(OIDC_HEADER, "")
                if raw:
                    try:
                        claims = verify_id_token(
                            raw, self.region, self.signer, self.issuer
                        )
                        state["officer"] = officer_from_claims(claims)
                    except AuthError as e:
                        log.warning(
                            "rejected sign-in on %s: %s", scope["path"], e.message
                        )
                        return await _reject(send, e.status, e.message)
                    _signed_in.set(True)
            else:
                state["officer"] = (
                    headers.get("x-watch-officer", "").strip() or DEFAULT_OFFICER
                )
            return await self.app(scope, receive, send)
        finally:
            _signed_in.reset(token)


def officer_of(state) -> str:
    """The identity to record: the signed-in officer, else the verified IAM caller."""
    officer = getattr(state, "officer", "") or ""
    if officer:
        return officer
    caller = getattr(state, "caller", None)
    if caller is not None and getattr(caller, "kind", "") == "agent":
        return f"iam:{caller.role}"
    if mode() == "oidc":
        raise AuthError(401, "sign in required")
    return DEFAULT_OFFICER
