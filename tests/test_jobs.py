"""Durable-job helpers (pure) and the A2A client's text extraction."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

WORKER = (Path(__file__).resolve().parents[1] / "services/api/worker.py").read_text()

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


# ---- stranded queued jobs ----
def test_the_worker_requeues_jobs_stranded_in_queued():
    """The job row is committed before its id is sent, so a failed or lost send leaves
    the row `queued` with nothing behind it: the worker never sees it, the watch floor
    shows the investigation queued forever, and because the idempotency key is unique
    only across `queued`/`running`, every later request for that vessel dedupes against
    the stranded row instead of starting work. Thirty accumulated over a day."""
    assert "def requeue_stranded(" in WORKER
    body = WORKER.split("def requeue_stranded(", 1)[1].split("\ndef ", 1)[0]
    assert "status='queued'" in body
    # only this worker's kinds, and only rows old enough to be genuinely stranded
    assert "KINDS" in body
    assert "STRANDED_GRACE_S" in body
    # each row exactly once: selecting on status alone re-sent the same ids every minute
    # and buried the queue in duplicates (129 deep in production before this)
    assert "requeue" in body and "progress @>" in body
    # and never via not_before, which the claim query treats as "not yet runnable"
    sql = body.split('"""', 1)[1].split('"""', 1)[0] if '"""' in body else body
    assert "not_before" not in sql
    # and it must actually run in the loop
    loop = WORKER.split("while not stop.is_set():", 1)[1][:400]
    assert "requeue_stranded()" in loop


def test_the_stranded_grace_is_longer_than_a_normal_queue_delay():
    """Re-sending a job that is merely waiting would duplicate work, so the grace period
    has to sit well beyond a healthy queue delay."""
    import re

    grace = int(re.search(r'JOB_STRANDED_GRACE_S", "(\d+)"', WORKER).group(1))
    assert grace >= 300


# ---- the id a queue is given ----
def test_both_queues_stringify_the_job_id():
    """psycopg returns `jobs.id` as a UUID object. `json.dumps` raises `TypeError:
    Object of type UUID is not JSON serializable` and redis-py raises `DataError`, both
    *before* the message is sent — so `open_investigation` committed the row, raised,
    and returned 500 while the job sat `queued` with attempts=0 forever and nothing
    was ever logged by SQS or botocore. Sweeps were unaffected only because the
    worker's own sends already stringified."""
    src = (Path(__file__).resolve().parents[1] / "services/api/jobqueue.py").read_text()
    for cls in ("class RedisQueue", "class SqsQueue"):
        body = src.split(cls, 1)[1].split("\nclass ", 1)[0]
        send = body.split("def send(", 1)[1].split("\n    def ", 1)[0]
        assert "str(job_id)" in send, cls
