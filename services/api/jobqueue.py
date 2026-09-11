"""Durable jobs (phase 3): the database row is the job; the queue only carries its id.

Backends (JOB_BACKEND): `redis` (Redis Streams with a consumer group, local default) and `sqs`
(AWS; the dead-letter queue is SQS redrive). Both give at-least-once delivery, which is safe
because a job row is claimed with a conditional UPDATE before it runs, and every handler is
idempotent (the orchestrator persists the report by investigation id).

Progress events for the UI go to the Redis stream `argus:events` on both platforms (Valkey on
AWS); the API forwards them as server-sent events on /events."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("jobqueue")

JOBS_STREAM = "argus:jobs"
DEAD_STREAM = "argus:jobs:dead"
DELAYED_ZSET = "argus:jobs:delayed"
EVENTS_STREAM = "argus:events"
GROUP = "workers"

DEFAULT_TIMEOUTS = {"sweep": 600, "investigation": 1200}
# Visibility timeout = the longest job this worker may run + a margin. The ladder that keeps
# a crashed worker's job retryable (ADR-0016): node timeouts x depth < job timeout <
# visibility < job timeout + reap grace. `ladder_ok` is pinned by a unit test.
VISIBILITY_MARGIN_S = 60


def queue_kinds() -> tuple[str, ...]:
    """Which job kinds this worker consumes: WORKER_QUEUE_KIND=sweep|investigation|all."""
    kind = os.getenv("WORKER_QUEUE_KIND", "all").strip().lower()
    return tuple(DEFAULT_TIMEOUTS) if kind in ("", "all") else (kind,)


def visibility_for(kinds: tuple[str, ...] | list[str]) -> int:
    return max(DEFAULT_TIMEOUTS[k] for k in kinds) + VISIBILITY_MARGIN_S


def ladder_ok(
    node_timeout_s: int, depth: int, timeout_s: int, visibility_s: int, grace_s: int
) -> bool:
    """The orchestrator's sequential node budget fits the job, the job fits the message
    visibility, and the reaper only fires after the queue has had its chance to redeliver."""
    return node_timeout_s * depth < timeout_s < visibility_s < timeout_s + grace_s


def backoff_seconds(attempt: int, base: int = 30, cap: int = 600) -> int:
    """30 s, 60 s, 120 s ... capped. attempt is the number of attempts already made."""
    return min(cap, base * (2 ** max(attempt - 1, 0)))


def sweep_key(now: datetime, interval_min: int, hours: float) -> str:
    """Idempotency key for scheduled sweeps: one per interval bucket, so a scheduler that fires
    twice (or two schedulers) cannot queue the same sweep twice."""
    bucket = (
        int(now.timestamp() // (interval_min * 60))
        if interval_min > 0
        else int(now.timestamp())
    )
    return f"sweep:{hours:g}h:{bucket}"


def investigation_key(
    mmsi: int, trigger: str, alert_id: str | None, now: datetime, window_h: int = 6
) -> str:
    """One investigation per vessel and trigger within a window; a specific alert id is stricter."""
    if alert_id:
        return f"investigation:{mmsi}:alert:{alert_id}"
    bucket = int(now.timestamp() // (window_h * 3600))
    return f"investigation:{mmsi}:{trigger}:{bucket}"


def is_transient(exc: BaseException) -> bool:
    """Retry on connectivity and server-side failures; not on 4xx or validation errors."""
    name = type(exc).__name__
    if name in (
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "RemoteProtocolError",
        "TimeoutError",
        "ConnectionError",
    ):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status >= 500 or status == 429
    code = (
        getattr(getattr(exc, "response", {}), "get", lambda *_: None)("Error", {}).get(
            "Code"
        )
        if hasattr(exc, "response")
        else None
    )
    return code in (
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
    )


@dataclass
class Delivery:
    receipt: Any
    job_id: str


class RedisQueue:
    def __init__(self, url: str):
        import redis

        # A blocking XREADGROUP must be allowed to outlive the client's socket timeout.
        self.r = redis.from_url(url, socket_timeout=None, socket_keepalive=True)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        try:
            self.r.xgroup_create(JOBS_STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:  # BUSYGROUP: already exists
            if "BUSYGROUP" not in str(e):
                raise

    def send(self, job_id: str, delay_s: int = 0) -> None:
        # str() for the same reason as SqsQueue.send: psycopg hands back a UUID object
        # and redis-py raises `DataError: Invalid input of type: 'UUID'`, so the local
        # backend fails exactly where AWS did.
        job_id = str(job_id)
        if delay_s > 0:
            self.r.zadd(DELAYED_ZSET, {job_id: time.time() + delay_s})
        else:
            self.r.xadd(JOBS_STREAM, {"job_id": job_id}, maxlen=10000, approximate=True)

    def promote_delayed(self) -> int:
        due = self.r.zrangebyscore(DELAYED_ZSET, 0, time.time())
        for job_id in due:
            if self.r.zrem(DELAYED_ZSET, job_id):
                self.r.xadd(
                    JOBS_STREAM,
                    {
                        "job_id": job_id.decode()
                        if isinstance(job_id, bytes)
                        else job_id
                    },
                )
        return len(due)

    def receive(
        self, block_ms: int = 5000, reclaim_idle_ms: int = 900_000
    ) -> list[Delivery]:
        # Reclaim messages a dead worker left pending longer than the longest job timeout.
        try:
            _, claimed, _ = self.r.xautoclaim(
                JOBS_STREAM,
                GROUP,
                self.consumer,
                min_idle_time=reclaim_idle_ms,
                start_id="0-0",
                count=10,
            )
        except Exception:  # noqa: BLE001
            claimed = []
        out = [Delivery(mid, fields[b"job_id"].decode()) for mid, fields in claimed]
        res = self.r.xreadgroup(
            GROUP, self.consumer, {JOBS_STREAM: ">"}, count=5, block=block_ms
        )
        for _, entries in res or []:
            for mid, fields in entries:
                out.append(Delivery(mid, fields[b"job_id"].decode()))
        return out

    def ack(self, d: Delivery) -> None:
        self.r.xack(JOBS_STREAM, GROUP, d.receipt)

    def extend(self, d: Delivery, seconds: int) -> None:
        """Redis reclaims by idle time; a worker that is still alive keeps the entry
        pending by touching it (XCLAIM to itself resets the idle clock)."""
        try:
            self.r.xclaim(
                JOBS_STREAM, GROUP, self.consumer, 0, [d.receipt], justid=True
            )
        except Exception:  # noqa: BLE001
            pass

    def dead(self, d: Delivery, reason: str) -> None:
        self.r.xadd(
            DEAD_STREAM,
            {
                "job_id": d.job_id,
                "reason": reason[:500],
                "ts": datetime.now(UTC).isoformat(),
            },
            maxlen=10000,
            approximate=True,
        )
        self.ack(d)


class SqsQueue:
    def __init__(self, queue_url: str):
        import boto3

        self.sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"))
        self.url = queue_url

    def send(self, job_id: str, delay_s: int = 0) -> None:
        # str() here, not at the call sites: psycopg returns `id` as a UUID object, and
        # `json.dumps` raises `TypeError: Object of type UUID is not JSON serializable`
        # *before* the message reaches SQS. The worker's own sends already stringified,
        # so sweeps worked while every investigation queued from the API raised after
        # its row was committed: the job sat `queued` with attempts=0 forever, the
        # officer saw a 500, and no SQS or botocore error was ever logged because the
        # call never got that far.
        self.sqs.send_message(
            QueueUrl=self.url,
            MessageBody=json.dumps({"job_id": str(job_id)}),
            DelaySeconds=min(delay_s, 900),
        )

    def promote_delayed(self) -> int:
        return 0  # SQS delays natively

    def receive(
        self, block_ms: int = 5000, reclaim_idle_ms: int = 900_000
    ) -> list[Delivery]:
        res = self.sqs.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=min(20, max(1, block_ms // 1000)),
            VisibilityTimeout=reclaim_idle_ms // 1000,
        )
        # EventBridge Scheduler messages ({"schedule": "sweep", ...}) carry no job id; the worker
        # turns them into an idempotent job row (worker.handle_schedule).
        out = []
        for m in res.get("Messages", []):
            body = json.loads(m["Body"])
            out.append(
                Delivery(m["ReceiptHandle"], body.get("job_id") or json.dumps(body))
            )
        return out

    def ack(self, d: Delivery) -> None:
        self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=d.receipt)

    def extend(self, d: Delivery, seconds: int) -> None:
        """Heartbeat: keep the message invisible while the job is still running here."""
        self.sqs.change_message_visibility(
            QueueUrl=self.url, ReceiptHandle=d.receipt, VisibilityTimeout=int(seconds)
        )

    def dead(self, d: Delivery, reason: str) -> None:
        # Redrive to the DLQ happens when the message is received more than maxReceiveCount times;
        # we simply stop deleting it. The job row already says why.
        self.sqs.change_message_visibility(
            QueueUrl=self.url, ReceiptHandle=d.receipt, VisibilityTimeout=0
        )


def make_queue(url_env: str = "JOB_QUEUE_URL"):
    backend = os.getenv("JOB_BACKEND", "redis").lower()
    if backend == "sqs":
        return SqsQueue(os.environ[url_env])
    return RedisQueue(os.getenv("REDIS_URL", "redis://redis:6379/0"))


def queue_url_env(kind: str) -> str:
    """Sweeps and investigations have their own queues on AWS (ADR-0016) so a burst of
    investigations cannot starve a timed sweep; locally both kinds share one stream."""
    if kind == "sweep" and os.getenv("JOB_SWEEP_QUEUE_URL"):
        return "JOB_SWEEP_QUEUE_URL"
    return "JOB_QUEUE_URL"


class Events:
    """Progress events for the UI, on the shared Redis/Valkey stream."""

    def __init__(self, url: str | None = None):
        import redis

        self.r = redis.from_url(url or os.getenv("REDIS_URL", "redis://redis:6379/0"))

    def publish(self, **event) -> None:
        event.setdefault("ts", datetime.now(UTC).isoformat())
        try:
            self.r.xadd(
                EVENTS_STREAM,
                {"data": json.dumps(event)},
                maxlen=5000,
                approximate=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("event publish failed: %s", e)
