"""Caller-identity verification: pure parts only (no STS, no SDK)."""

import sys
import time

import pytest

sys.path.insert(0, "mcp-servers")
from common.callerauth import (  # noqa: E402
    STS_BODY,
    AuthError,
    Caller,
    authorize,
    decode_token,
    encode_token,
    is_sts_url,
    parse_sts_response,
    role_name_from_arn,
    token_age_s,
)


def _headers(**over) -> dict:
    h = {
        "Authorization": "AWS4-HMAC-SHA256 Credential=AKIA/20260904/us-east-1/sts/aws4_request, SignedHeaders=host;x-amz-date, Signature=abc",
        "X-Amz-Date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    h.update(over)
    return h


def test_token_roundtrip():
    tok = encode_token(
        "POST", "https://sts.us-east-1.amazonaws.com/", _headers(), STS_BODY
    )
    req = decode_token(tok)
    assert req["url"] == "https://sts.us-east-1.amazonaws.com/"
    assert req["body"] == STS_BODY
    assert "authorization" in req["headers"]


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://sts.us-east-1.amazonaws.com/", True),
        ("https://sts.amazonaws.com/", True),
        ("https://sts.eu-west-2.amazonaws.com/", True),
        ("http://sts.us-east-1.amazonaws.com/", False),
        ("https://sts.us-east-1.amazonaws.com.evil.example/", False),
        ("https://evil.example/?host=sts.amazonaws.com", False),
    ],
)
def test_only_https_sts_hosts(url, ok):
    assert is_sts_url(url) is ok


def test_rejects_wrong_body_method_headers_and_age():
    with pytest.raises(AuthError):
        decode_token(
            encode_token(
                "POST", "https://sts.amazonaws.com/", _headers(), "Action=AssumeRole"
            )
        )
    with pytest.raises(AuthError):
        decode_token(
            encode_token("GET", "https://sts.amazonaws.com/", _headers(), STS_BODY)
        )
    with pytest.raises(AuthError):
        decode_token(
            encode_token(
                "POST",
                "https://sts.amazonaws.com/",
                _headers(**{"X-Evil": "1"}),
                STS_BODY,
            )
        )
    old = _headers(**{"X-Amz-Date": "20200101T000000Z"})
    with pytest.raises(AuthError):
        decode_token(encode_token("POST", "https://sts.amazonaws.com/", old, STS_BODY))
    with pytest.raises(AuthError):
        decode_token("not-a-token")


def test_token_age():
    import calendar

    t0 = calendar.timegm(time.strptime("20260904T120000Z", "%Y%m%dT%H%M%SZ"))
    assert token_age_s("20260904T120000Z", now=t0 + 60) == pytest.approx(60)
    assert token_age_s("garbage") == float("inf")


@pytest.mark.parametrize(
    "arn,role",
    [
        (
            "arn:aws:sts::123456789012:assumed-role/argus-agent-watch/session-1",
            "argus-agent-watch",
        ),
        (
            "arn:aws:iam::123456789012:role/argus-agent-orchestrator",
            "argus-agent-orchestrator",
        ),
        ("arn:aws:iam::123456789012:user/alice", "alice"),
        ("arn:aws:iam::123456789012:root", ""),
        ("nonsense", ""),
    ],
)
def test_role_name_from_arn(arn, role):
    assert role_name_from_arn(arn) == role


def test_parse_sts_response():
    xml = "<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>arn:aws:sts::123456789012:assumed-role/argus-agent-watch/s</Arn><UserId>X</UserId><Account>123456789012</Account></GetCallerIdentityResult></GetCallerIdentityResponse>"
    assert parse_sts_response(xml) == (
        "arn:aws:sts::123456789012:assumed-role/argus-agent-watch/s",
        "123456789012",
    )
    with pytest.raises(AuthError):
        parse_sts_response("<Error/>")


def test_authorize_fails_closed():
    watch = Caller(
        arn="a", account="123456789012", role="argus-agent-watch", kind="agent"
    )
    authorize(watch, {"argus-agent-watch", "argus-agent-investigator"})
    with pytest.raises(AuthError) as e:
        authorize(watch, set())
    assert e.value.status == 403
    with pytest.raises(AuthError):
        authorize(watch, {"argus-agent-watch"}, account="999999999999")
    with pytest.raises(AuthError):
        authorize(
            Caller("a", "1", "argus-agent-tasking", "agent"), {"argus-agent-watch"}
        )
    # a principal we could not map to a role name (e.g. account root) is never allowed
    with pytest.raises(AuthError):
        authorize(Caller("arn:aws:iam::1:root", "1", "", "agent"), {""})


def test_route_matcher():
    from common.callerauth import matches_route

    routes = (("POST", "/alerts"), ("POST", "/investigations/*/complete"))
    assert matches_route(routes, "/alerts", "POST")
    assert matches_route(routes, "/investigations/abc-123/complete", "POST")
    assert not matches_route(routes, "/alerts", "GET")
    assert not matches_route(routes, "/alerts/abc/review", "POST")
    assert not matches_route(routes, "/investigations/abc/complete/extra", "POST")


def test_both_copies_identical():
    a = open("mcp-servers/common/callerauth.py").read()
    b = open("services/api/callerauth.py").read()
    assert a == b, (
        "callerauth.py must stay identical in mcp-servers/common and services/api"
    )
