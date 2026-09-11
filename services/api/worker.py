"""Job worker (phase 3): runs sweeps and investigations from the durable queue, with retries,
backoff, timeouts, a dead-letter path, and progress events for the UI.

    python worker.py

Sweeps call the Watch agent directly over A2A. Investigations invoke the orchestrator (local
container or AgentCore runtime); the orchestrator persists the report itself through the API.
With SWEEP_INTERVAL_MIN > 0 this process also acts as the local scheduler; on AWS EventBridge
Scheduler enqueues sweeps on the same idempotency key and this loop stays idle as a scheduler."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from datetime import UTC, datetime
from typing import Any

import runtime_client
from a2a_client import make_sigv4, send_message
from dbconn import RotatingPool
from jobqueue import (
    DEFAULT_TIMEOUTS,
    VISIBILITY_MARGIN_S,
    Events,
    backoff_seconds,
    is_transient,
    ladder_ok,
    make_queue,
    queue_kinds,
    sweep_key,
    visibility_for,
)
from psycopg.types.json import Jsonb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")


pool = RotatingPool(
    min_size=1, max_size=max(4, int(os.getenv("WORKER_CONCURRENCY", "2")) + 2)
)
queue = make_queue()  # JOB_QUEUE_URL is this worker's own queue (per kind on AWS)
events = Events()
SWEEP_INTERVAL_MIN = int(os.getenv("SWEEP_INTERVAL_MIN", "0"))
# Jobs run concurrently up to this many; each investigation is one AgentCore invocation.
WORKER_CONCURRENCY = max(1, int(os.getenv("WORKER_CONCURRENCY", "2")))
SWEEP_HOURS = float(os.getenv("SWEEP_HOURS", "12"))
WATCH_URL = os.getenv("A2A_WATCH_URL", "http://agent-watch:9000")
A2A_AUTH = os.getenv("A2A_AUTH", "none")
REGION = os.getenv("AWS_REGION", "us-east-1")
stop = threading.Event()


def q(sql: str, params: tuple = ()) -> list[dict]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows


def audit(
    actor: str,
    actor_kind: str,
    action: str,
    entity_kind: str,
    entity_id: str | None,
    details: dict | None = None,
    trace_id: str | None = None,
) -> None:
    q(
        "INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details, trace_id, manifest_ref) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            actor,
            actor_kind,
            action,
            entity_kind,
            entity_id,
            Jsonb(details or {}),
            trace_id,
            trace_id,
        ),
    )


def progress(job: dict, step: str, status: str, detail: str = "") -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "step": step,
        "status": status,
        "detail": detail[:500],
    }
    q(
        "UPDATE jobs SET progress = progress || %s::jsonb, updated_at = now() WHERE id=%s",
        (Jsonb([entry]), job["id"]),
    )
    events.publish(
        type="job",
        job_id=str(job["id"]),
        kind=job["kind"],
        investigation_id=job["payload"].get("investigation_id"),
        mmsi=job["payload"].get("mmsi"),
        **entry,
    )


# ---------------- handlers ----------------
def watch_auth():
    return make_sigv4(REGION) if A2A_AUTH == "sigv4" else None


def watch_url() -> str:
    """The Watch agent's A2A URL: the AWS Agent Registry record first (ADR-0013), then the
    SSM parameter the agents stack writes, then the environment."""
    global WATCH_URL
    if WATCH_URL:
        return WATCH_URL
    from_registry = registry_agent_url("watch")
    if from_registry:
        WATCH_URL = from_registry
        return WATCH_URL
    import boto3

    WATCH_URL = boto3.client("ssm", region_name=REGION).get_parameter(
        Name=os.getenv("A2A_WATCH_URL_PARAM", "/argus/watch-a2a-url")
    )["Parameter"]["Value"]
    return WATCH_URL


def registry_agent_url(name: str) -> str | None:
    """Approved record `argus-agent-<name>` in the registry named by ARGUS_REGISTRY_ID (or
    the SSM parameter /argus/registry-id); None when unavailable."""
    registry_id = os.getenv("ARGUS_REGISTRY_ID")
    try:
        import boto3

        if not registry_id:
            registry_id = boto3.client("ssm", region_name=REGION).get_parameter(
                Name="/argus/registry-id"
            )["Parameter"]["Value"]
        c = boto3.client("agent-registry", region_name=REGION)
        # listing returns names and status; the card comes from a batch get
        token = None
        while True:
            kwargs = {"registryId": registry_id, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            page = c.list_discoverable_registry_records(**kwargs)
            for rec in page.get("registryRecords", []):
                if (
                    rec.get("name") != f"argus-agent-{name}"
                    or rec.get("status") != "APPROVED"
                ):
                    continue
                got = c.batch_get_discoverable_registry_record(
                    entries=[
                        {"registryId": registry_id, "recordIds": [rec["recordId"]]}
                    ]
                )
                for full in got.get("registryRecords", []):
                    data = (
                        full.get("descriptors", {}).get("a2aAgentCard", {}).get("data")
                    )
                    url = json.loads(data).get("url") if data else None
                    if url:
                        log.info("%s agent resolved from the registry: %s", name, url)
                        return url
            token = page.get("nextToken")
            if not token:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("registry lookup for %s failed: %s", name, e)
    return None


#: A draft alert nobody reviewed within this many hours leaves the queue (P5). Set
#: ALERT_EXPIRY_HOURS=0 to keep every draft open forever.
ALERT_EXPIRY_HOURS = int(os.getenv("ALERT_EXPIRY_HOURS", "72"))


def expire_stale_alerts() -> int:
    """Close drafts nobody reviewed, audited. Never raises: a sweep must still run.

    One day of live sweeps left 131 alerts awaiting review, which is a queue no officer
    can finish and therefore the same as no queue at all. Expiry is not deletion: the
    alert stays readable, it just stops being work.
    """
    if ALERT_EXPIRY_HOURS <= 0:
        return 0
    try:
        rows = q("SELECT expire_stale_alerts(%s) AS n", (ALERT_EXPIRY_HOURS,))
        n = int(rows[0]["n"] or 0) if rows else 0
        if n:
            log.info("expired %d alert(s) older than %d h", n, ALERT_EXPIRY_HOURS)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("alert expiry failed: %s", e)
        return 0


def run_sweep(job: dict) -> dict:
    hours = float(job["payload"].get("hours", SWEEP_HOURS))
    expire_stale_alerts()
    progress(job, "watch", "started", f"sweeping the last {hours:g} h")
    res = send_message(
        watch_url(),
        f"Sweep the last {hours:g} hours of AIS and raise alerts. Return your JSON summary.",
        auth=watch_auth(),
        timeout=job["timeout_s"],
    )
    summary: dict[str, Any] = {"text": res["text"][:4000]}
    try:
        start = res["text"].index("{")
        summary.update(json.loads(res["text"][start : res["text"].rindex("}") + 1]))
    except (ValueError, json.JSONDecodeError):
        pass
    progress(
        job, "watch", "finished", f"{summary.get('alerts_raised', '?')} alerts raised"
    )
    audit(
        "worker",
        "system",
        "sweep.completed",
        "sweep",
        str(job["id"]),
        {"hours": hours, "alerts_raised": summary.get("alerts_raised")},
    )
    return summary


def run_investigation(job: dict) -> dict:
    p = job["payload"]
    progress(job, "orchestrator", "started", "investigation handed to the orchestrator")
    result = runtime_client.invoke(
        {
            "mmsi": p["mmsi"],
            "trigger": p.get("trigger", "manual"),
            "investigation_id": p["investigation_id"],
            "alert": p.get("alert") or {},
            "requested_by": job.get("requested_by"),
        },
        session_id=runtime_client.runtime_session_id(p["investigation_id"]),
    )
    progress(job, "orchestrator", "finished", "report persisted")
    return {
        "trace_id": result.get("trace_id"),
        "priority": (result.get("report") or {}).get("priority"),
    }


HANDLERS = {"sweep": run_sweep, "investigation": run_investigation}


# ---------------- lifecycle ----------------
def claim(job_id: str) -> dict | None:
    rows = q(
        """UPDATE jobs SET status='running', attempts=attempts+1, started_at=now(), updated_at=now()
           WHERE id=%s AND status='queued' AND (not_before IS NULL OR not_before <= now()) RETURNING *""",
        (job_id,),
    )
    return rows[0] if rows else None


def reclaim(job_id: str) -> dict | None:
    """A redelivered message for a row still `running`: the worker that held it died
    (its heartbeat stopped, so the queue redelivered). Take it over while attempts
    remain and the row is older than its visibility window; otherwise leave it to the
    reaper. Without this, at-least-once delivery silently became at-most-once (B2)."""
    rows = q(
        """UPDATE jobs SET attempts=attempts+1, started_at=now(), updated_at=now(),
                  progress = progress || %s::jsonb
           WHERE id=%s AND status='running' AND attempts < max_attempts
             AND started_at < now() - make_interval(secs => timeout_s + %s)
           RETURNING *""",
        (
            Jsonb(
                [
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "step": "job",
                        "status": "reclaimed",
                        "detail": "previous worker stopped heartbeating",
                    }
                ]
            ),
            job_id,
            VISIBILITY_MARGIN_S,
        ),
    )
    return rows[0] if rows else None


def finish(job: dict, result: dict) -> None:
    q(
        "UPDATE jobs SET status='succeeded', result=%s, finished_at=now(), updated_at=now() WHERE id=%s",
        (Jsonb(result), job["id"]),
    )
    progress(job, "job", "succeeded")


def fail(job: dict, exc: BaseException, delivery) -> None:
    err = f"{type(exc).__name__}: {exc}"[:2000]
    retry = is_transient(exc) and job["attempts"] < job["max_attempts"]
    if retry:
        delay = backoff_seconds(job["attempts"])
        q(
            "UPDATE jobs SET status='queued', error=%s, not_before=now() + make_interval(secs => %s), updated_at=now() WHERE id=%s",
            (err, delay, job["id"]),
        )
        progress(
            job,
            "job",
            "retrying",
            f"attempt {job['attempts']} failed, retry in {delay}s: {err}",
        )
        queue.ack(delivery)
        queue.send(str(job["id"]), delay_s=delay)
        return
    status = "dead" if is_transient(exc) else "failed"
    q(
        "UPDATE jobs SET status=%s, error=%s, finished_at=now(), updated_at=now() WHERE id=%s",
        (status, err, job["id"]),
    )
    progress(job, "job", status, err)
    inv = job["payload"].get("investigation_id")
    if inv:
        q(
            "UPDATE investigations SET status='failed', report=%s, updated_at=now() WHERE id=%s AND status='running'",
            (Jsonb({"error": err}), inv),
        )
    audit(
        "worker",
        "system",
        f"job.{status}",
        "job",
        str(job["id"]),
        {"kind": job["kind"], "error": err, "attempts": job["attempts"]},
    )
    queue.dead(delivery, err) if status == "dead" else queue.ack(delivery)


def handle_schedule(delivery, body: dict) -> None:
    """An EventBridge Scheduler message: create the sweep job on the interval's idempotency key."""
    hours = float(body.get("hours", SWEEP_HOURS))
    interval = int(body.get("interval_min", 30))
    key = sweep_key(datetime.now(UTC), interval, hours)
    rows = q(
        """INSERT INTO jobs (kind, idempotency_key, payload, timeout_s, requested_by) VALUES ('sweep', %s, %s, %s, 'scheduler')
           ON CONFLICT (kind, idempotency_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id""",
        (key, Jsonb({"hours": hours}), DEFAULT_TIMEOUTS["sweep"]),
    )
    queue.ack(delivery)
    if rows:
        audit(
            "scheduler",
            "system",
            "sweep.requested",
            "sweep",
            str(rows[0]["id"]),
            {"hours": hours, "interval_min": interval},
        )
        queue.send(str(rows[0]["id"]))


HEARTBEAT_S = int(os.getenv("JOB_HEARTBEAT_S", "30"))


def wait_with_heartbeat(fut, job: dict, delivery):
    """Block on the handler while extending the message's visibility every HEARTBEAT_S,
    so a long agent run is never redelivered under a live worker and a dead worker's
    message reappears within one heartbeat interval of the visibility window (ADR-0016)."""
    deadline = time.time() + job["timeout_s"]
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise FutTimeout()
        try:
            return fut.result(timeout=min(HEARTBEAT_S, remaining))
        except FutTimeout:
            if time.time() >= deadline:
                raise
            try:
                queue.extend(delivery, VISIBILITY_S)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed for job %s: %s", job["id"], e)


def handle(delivery) -> None:
    if delivery.job_id.startswith("{"):
        body = json.loads(delivery.job_id)
        if body.get("schedule") == "sweep":
            handle_schedule(delivery, body)
        else:
            queue.ack(delivery)
        return
    job = claim(delivery.job_id) or reclaim(delivery.job_id)
    if job is None:
        rows = q("SELECT status, not_before FROM jobs WHERE id=%s", (delivery.job_id,))
        if rows and rows[0]["status"] == "queued" and rows[0]["not_before"]:
            queue.ack(delivery)
            queue.send(
                delivery.job_id,
                delay_s=max(
                    1, int((rows[0]["not_before"] - datetime.now(UTC)).total_seconds())
                ),
            )
        else:
            queue.ack(delivery)  # already running elsewhere, finished, or unknown
        return
    log.info("job %s %s attempt %d", job["kind"], job["id"], job["attempts"])
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(HANDLERS[job["kind"]], job)
        try:
            result = wait_with_heartbeat(fut, job, delivery)
        except FutTimeout:
            fail(job, TimeoutError(f"job exceeded {job['timeout_s']} s"), delivery)
            return
        except Exception as e:  # noqa: BLE001
            log.warning(
                "job %s failed: %s\n%s", job["id"], e, traceback.format_exc(limit=3)
            )
            fail(job, e, delivery)
            return
    finish(job, result)
    queue.ack(delivery)


def enqueue_sweep_if_due(last_bucket: list[int]) -> None:
    if SWEEP_INTERVAL_MIN <= 0:
        return
    now = datetime.now(UTC)
    bucket = int(now.timestamp() // (SWEEP_INTERVAL_MIN * 60))
    if last_bucket and last_bucket[0] == bucket:
        return
    last_bucket[:] = [bucket]
    key = sweep_key(now, SWEEP_INTERVAL_MIN, SWEEP_HOURS)
    rows = q(
        """INSERT INTO jobs (kind, idempotency_key, payload, timeout_s, requested_by) VALUES ('sweep', %s, %s, %s, 'scheduler')
           ON CONFLICT (kind, idempotency_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id""",
        (key, Jsonb({"hours": SWEEP_HOURS}), DEFAULT_TIMEOUTS["sweep"]),
    )
    if rows:
        queue.send(str(rows[0]["id"]))
        audit(
            "scheduler",
            "system",
            "sweep.requested",
            "sweep",
            str(rows[0]["id"]),
            {"hours": SWEEP_HOURS, "interval_min": SWEEP_INTERVAL_MIN},
        )
        log.info("scheduled sweep %s", rows[0]["id"])


REAP_GRACE_S = int(os.getenv("JOB_REAP_GRACE_S", "600"))
KINDS = queue_kinds()
VISIBILITY_S = visibility_for(KINDS)
NODE_TIMEOUT_S = int(os.getenv("NODE_TIMEOUT_S", "480"))
NODE_DEPTH = {"sweep": 1, "investigation": 2}  # sequential agent calls per job kind
for _kind in KINDS:
    if not ladder_ok(
        NODE_TIMEOUT_S,
        NODE_DEPTH[_kind],
        DEFAULT_TIMEOUTS[_kind],
        VISIBILITY_S,
        REAP_GRACE_S,
    ):
        log.error(
            "timeout ladder violated for %s: node %s x2 < job %s < visibility %s < job + grace %s",
            _kind,
            NODE_TIMEOUT_S,
            DEFAULT_TIMEOUTS[_kind],
            VISIBILITY_S,
            DEFAULT_TIMEOUTS[_kind] + REAP_GRACE_S,
        )
_last_reap = 0.0
_last_requeue = 0.0
#: A `queued` row older than this with no message behind it is stranded, not merely
#: waiting. Comfortably longer than a normal queue delay so a healthy job is never
#: re-sent.
STRANDED_GRACE_S = int(os.getenv("JOB_STRANDED_GRACE_S", "300"))


def reap_orphans() -> int:
    """Jobs still `running` long after their timeout were orphaned by a worker restart
    (the queue redelivers the message, but a job whose message was already acknowledged
    or lost keeps the row). Mark them failed so the watch floor and the SLIs stop counting
    them as work in progress. Runs at most once a minute; cheap and idempotent."""
    global _last_reap
    if time.time() - _last_reap < 60:
        return 0
    _last_reap = time.time()
    rows = q(
        """UPDATE jobs SET status='failed', error=%s, finished_at=now(), updated_at=now()
           WHERE status='running' AND started_at < now() - make_interval(secs => timeout_s + %s)
           RETURNING id, kind, payload, attempts""",
        (
            "orphaned: no worker finished it within its timeout (worker restart)",
            REAP_GRACE_S,
        ),
    )
    for job in rows:
        inv = (job.get("payload") or {}).get("investigation_id")
        if inv:
            q(
                "UPDATE investigations SET status='failed', report=%s, updated_at=now() WHERE id=%s AND status='running'",
                (Jsonb({"error": "orphaned by a worker restart"}), inv),
            )
        audit(
            "worker",
            "system",
            "job.failed",
            "job",
            str(job["id"]),
            {"kind": job["kind"], "error": "orphaned", "attempts": job["attempts"]},
        )
        log.warning("reaped orphaned %s job %s", job["kind"], job["id"])
    return len(rows)


def requeue_stranded() -> int:
    """Re-send jobs that are `queued` in the database but absent from the queue.

    `enqueue`/`open_investigation` commit the job row and only then send its id, so a
    send that fails (or a message dropped before delivery) strands the row: the worker
    never sees it, the watch floor shows the investigation queued forever, and — because
    the idempotency key is only unique across `queued`/`running` — every later request
    for that vessel dedupes against the stranded row instead of starting work. Thirty of
    them accumulated over a day before this existed.

    Re-sending is safe: delivery is at-least-once already, so a duplicate message for a
    row that is genuinely in flight is the case handlers are written for. Only rows older
    than the grace period are touched, so a job queued seconds ago is left alone.
    """
    global _last_requeue
    if time.time() - _last_requeue < 60:
        return 0
    _last_requeue = time.time()
    # A stranded row keeps `status='queued'` until a worker claims it, so selecting on
    # status alone re-sent the same ids every minute and buried the queue in duplicates
    # (129 messages deep before this clause existed). `not_before` cannot be the cooldown:
    # the claim query refuses a job whose `not_before` is in the future, so stamping it
    # would block the very rows being rescued. Mark the attempt in `progress` instead,
    # which is advisory and read by nothing that gates execution.
    rows = q(
        """UPDATE jobs
              SET progress = progress || %s::jsonb, updated_at = now()
            WHERE id IN (
              SELECT id FROM jobs
               WHERE status='queued' AND kind = ANY(%s)
                 AND created_at < now() - make_interval(secs => %s)
                 AND NOT (progress @> %s::jsonb)
               ORDER BY created_at LIMIT 25
            )
            RETURNING id""",
        (
            json.dumps([{"step": "requeue", "status": "sent"}]),
            list(KINDS),
            STRANDED_GRACE_S,
            json.dumps([{"step": "requeue"}]),
        ),
    )
    for job in rows:
        try:
            queue.send(str(job["id"]))
            log.warning("requeued stranded job %s", job["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("could not requeue job %s: %s", job["id"], e)
    return len(rows)


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info(
        "worker up: backend=%s sweep_interval=%s min watch=%s",
        os.getenv("JOB_BACKEND", "redis"),
        SWEEP_INTERVAL_MIN,
        WATCH_URL or "(ssm)",
    )
    last_bucket: list[int] = []
    inflight: set = set()
    runner = ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY)
    log.info(
        "worker concurrency: %d kinds=%s visibility=%ss",
        WORKER_CONCURRENCY,
        KINDS,
        VISIBILITY_S,
    )
    while not stop.is_set():
        try:
            enqueue_sweep_if_due(last_bucket)
            reap_orphans()
            requeue_stranded()
            queue.promote_delayed()
            inflight = {f for f in inflight if not f.done()}
            free = WORKER_CONCURRENCY - len(inflight)
            if free <= 0:
                time.sleep(1)
                continue
            for d in queue.receive(block_ms=5000, reclaim_idle_ms=VISIBILITY_S * 1000)[
                :free
            ]:
                inflight.add(runner.submit(handle, d))
        except Exception as e:  # noqa: BLE001
            log.error("worker loop error: %s", e)
            time.sleep(5)
    runner.shutdown(wait=True)


if __name__ == "__main__":
    main()
