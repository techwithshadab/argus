"""Durable-job helpers (pure) and the A2A client's text extraction."""

import sys
from datetime import UTC, datetime

import pytest

sys.path.insert(0, "services/api")
from a2a_client import extract_text  # noqa: E402
from jobqueue import (  # noqa: E402
    DEFAULT_TIMEOUTS,
    VISIBILITY_MARGIN_S,
    backoff_seconds,
    investigation_key,
    is_transient,
    ladder_ok,
    queue_kinds,
    queue_url_env,
    sweep_key,
    visibility_for,
)


def test_backoff_doubles_and_caps():
    assert [backoff_seconds(a) for a in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(10) == 600


def test_sweep_key_is_one_per_interval_bucket():
    t0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    t1 = datetime(2026, 9, 1, 8, 9, tzinfo=UTC)
    t2 = datetime(2026, 9, 1, 8, 11, tzinfo=UTC)
    assert sweep_key(t0, 10, 12) == sweep_key(t1, 10, 12)
    assert sweep_key(t0, 10, 12) != sweep_key(t2, 10, 12)
    # a manual sweep (interval 0) is its own key every second
    assert sweep_key(t0, 0, 12) != sweep_key(t1, 0, 12)


def test_investigation_key_prefers_alert_id_then_window():
    t = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert investigation_key(1, "ais_gap", "abc", t) == "investigation:1:alert:abc"
    a = investigation_key(1, "ais_gap", None, t)
    # 6-hour buckets are aligned to the epoch (00, 06, 12, 18 UTC)
    b = investigation_key(1, "ais_gap", None, datetime(2026, 9, 1, 11, 59, tzinfo=UTC))
    c = investigation_key(1, "ais_gap", None, datetime(2026, 9, 1, 12, 1, tzinfo=UTC))
    assert a == b and a != c
    assert investigation_key(1, "manual", None, t) != a


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _HttpErr(Exception):
    def __init__(self, status):
        self.response = _Resp(status)


@pytest.mark.parametrize(
    "exc,transient",
    [
        (TimeoutError("t"), True),
        (ConnectionError("c"), True),
        (_HttpErr(503), True),
        (_HttpErr(429), True),
        (_HttpErr(400), False),
        (_HttpErr(404), False),
        (ValueError("bad payload"), False),
    ],
)
def test_transient_classification(exc, transient):
    assert is_transient(exc) is transient


def test_extract_text_walks_messages_and_tasks():
    msg = {
        "kind": "message",
        "parts": [{"kind": "text", "text": "hello"}, {"kind": "data", "data": {}}],
    }
    task = {
        "kind": "task",
        "status": {
            "state": "completed",
            "message": {"parts": [{"kind": "text", "text": "done"}]},
        },
        "artifacts": [{"parts": [{"kind": "text", "text": '{"alerts_raised": 2}'}]}],
    }
    assert extract_text(msg) == "hello"
    assert extract_text(task) == 'done\n{"alerts_raised": 2}'
    assert extract_text({"parts": []}) == ""


def test_timeout_ladder_holds_for_every_kind(monkeypatch):
    """node timeouts x depth < job timeout < visibility < job timeout + reap grace (ADR-0016)."""
    depth = {"sweep": 1, "investigation": 2}  # sequential agent calls per job kind
    for kind, timeout in DEFAULT_TIMEOUTS.items():
        vis = visibility_for((kind,))
        assert vis == timeout + VISIBILITY_MARGIN_S
        assert ladder_ok(480, depth[kind], timeout, vis, 600), kind
    assert not ladder_ok(
        480, 2, 900, 960, 180
    )  # the pre-ADR ladder: reap before redelivery


def test_queue_per_kind_and_worker_kinds(monkeypatch):
    monkeypatch.delenv("JOB_SWEEP_QUEUE_URL", raising=False)
    assert queue_url_env("sweep") == "JOB_QUEUE_URL"
    monkeypatch.setenv("JOB_SWEEP_QUEUE_URL", "https://sqs/argus-sweeps")
    assert queue_url_env("sweep") == "JOB_SWEEP_QUEUE_URL"
    assert queue_url_env("investigation") == "JOB_QUEUE_URL"
    monkeypatch.delenv("WORKER_QUEUE_KIND", raising=False)
    assert set(queue_kinds()) == set(DEFAULT_TIMEOUTS)
    monkeypatch.setenv("WORKER_QUEUE_KIND", "sweep")
    assert queue_kinds() == ("sweep",) and visibility_for(queue_kinds()) == 660
