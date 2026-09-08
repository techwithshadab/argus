"""Platform API: vessels, tracks, alerts, investigations, tasking approvals, live stream."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
import review_metrics
import run_metrics
import sli_pure
from callerauth import CallerAuthMiddleware, matches_route
from datakey import decrypting
from dbconn import RotatingPool
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from jobqueue import (
    DEFAULT_TIMEOUTS,
    EVENTS_STREAM,
    Events,
    investigation_key,
    make_queue,
    queue_url_env,
    sweep_key,
)
from officerauth import (
    AuthError,
    OfficerAuthMiddleware,
    needs_iam,
    officer_of,
)
from officerauth import mode as auth_mode
from psycopg.types.json import Jsonb
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="Argus API", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Routes that only agents call. With TOOL_AUTH=aws-iam they need a verified IAM identity
# in TOOL_ALLOWED_ROLES. Everything else is the watch floor: with OFFICER_AUTH=oidc (AWS)
# it needs the load balancer's signed id token or an IAM caller, with OFFICER_AUTH=header
# (local) the X-Watch-Officer header names the officer (officerauth.py, ADR-0018).
_AGENT_ROUTES = (
    ("POST", "/alerts"),
    ("POST", "/investigations/*/complete"),
    ("POST", "/investigations/*/fail"),
    ("POST", "/investigations/*/progress"),
)


def is_agent_route(path: str, method: str) -> bool:
    return matches_route(_AGENT_ROUTES, path, method)


def requires_iam(path: str, method: str) -> bool:
    return needs_iam(path, method, is_agent_route)


app.add_middleware(CallerAuthMiddleware, protected=requires_iam)
app.add_middleware(OfficerAuthMiddleware)  # outermost: decides before caller auth


def current_officer(request: Request) -> str:
    """The identity recorded on a decision (see officerauth.officer_of)."""
    try:
        return officer_of(request.state)
    except AuthError as e:
        raise HTTPException(e.status, e.message) from e


pool = RotatingPool(min_size=1, max_size=10)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
SCENARIO_END = os.getenv("SCENARIO_END", "2026-09-01T08:00:00Z")


def scenario_now() -> str:
    """The clock every window is measured from: the scenario's end in replay, the wall clock
    in live mode (AIS_MODE=live), as ISO-8601 UTC."""
    if os.getenv("AIS_MODE", "replay").strip().lower() == "live":
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SCENARIO_END


# ---- telemetry ----
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        tp = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "api",
                    "service.namespace": "argus",
                    "deployment.environment": os.getenv("DEPLOY_ENV", "local"),
                }
            )
        )
        tp.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'].rstrip('/')}/v1/traces"
                )
            )
        )
        trace.set_tracer_provider(tp)
        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        PsycopgInstrumentor().instrument()
except Exception as e:  # noqa: BLE001
    log.warning("telemetry disabled: %s", e)


def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        conn.commit()
        return [_ser(dict(r)) for r in rows]


def _ser(d: dict) -> dict:
    return {
        k: (
            v.isoformat()
            if isinstance(v, datetime)
            else (str(v) if v.__class__.__name__ == "UUID" else v)
        )
        for k, v in d.items()
    }


def audit(
    actor: str,
    actor_kind: str,
    action: str,
    entity_kind: str,
    entity_id: str | None,
    details: dict | None = None,
    trace_id: str | None = None,
) -> None:
    """Append-only audit event (see data/sql/002_review_audit.sql). Never raises past the caller."""
    q(
        """INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details, trace_id, manifest_ref)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
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


def agent_actor(request: Request, fallback: str) -> str:
    """Verified IAM role of the calling agent, else the name the agent claims (auth off)."""
    caller = getattr(request.state, "caller", None)
    return caller.role if caller is not None and caller.kind == "agent" else fallback


REVIEW_DECISIONS = ("accepted", "rejected")

# ---- durable jobs (phase 3): the row is the job, the queue carries its id ----
queues = {k: make_queue(queue_url_env(k)) for k in DEFAULT_TIMEOUTS}
queue = queues["investigation"]
events = Events()
AUTO_INVESTIGATE = {
    s.strip()
    for s in os.getenv("AUTO_INVESTIGATE_SEVERITIES", "high").split(",")
    if s.strip()
}


def enqueue(kind: str, key: str, payload: dict, requested_by: str) -> tuple[str, bool]:
    """Idempotent: an active job with the same key is returned instead of a second one."""
    rows = q(
        """INSERT INTO jobs (kind, idempotency_key, payload, timeout_s, requested_by) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (kind, idempotency_key) WHERE status IN ('queued', 'running') DO NOTHING RETURNING id""",
        (kind, key, Jsonb(payload), DEFAULT_TIMEOUTS[kind], requested_by),
    )
    if rows:
        job_id = rows[0]["id"]
        queues[kind].send(job_id)
        events.publish(
            type="job",
            job_id=job_id,
            kind=kind,
            step="job",
            status="queued",
            investigation_id=payload.get("investigation_id"),
            mmsi=payload.get("mmsi"),
        )
        return job_id, True
    existing = q(
        "SELECT id FROM jobs WHERE kind=%s AND idempotency_key=%s AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
        (kind, key),
    )
    return existing[0]["id"], False


def open_investigation(
    mmsi: int, trigger: str, alert: dict | None, requested_by: str, actor_kind: str
) -> dict:
    """Create the investigation row and its job. Returns {investigation_id, job_id, deduplicated}."""
    alert_id = (alert or {}).get("id")
    key = investigation_key(mmsi, trigger, alert_id, datetime.now(UTC))
    active = q(
        "SELECT id FROM jobs WHERE kind='investigation' AND idempotency_key=%s AND status IN ('queued','running')",
        (key,),
    )
    if active:
        inv = q("SELECT id FROM investigations WHERE job_id=%s", (active[0]["id"],))
        return {
            "investigation_id": inv[0]["id"] if inv else None,
            "job_id": active[0]["id"],
            "deduplicated": True,
        }
    rows = q(
        "INSERT INTO investigations (mmsi, trigger, alert_id, requested_by) VALUES (%s,%s,%s,%s) RETURNING id",
        (mmsi, trigger, alert_id, requested_by),
    )
    inv_id = rows[0]["id"]
    job_id, _ = enqueue(
        "investigation",
        key,
        {
            "mmsi": mmsi,
            "trigger": trigger,
            "investigation_id": inv_id,
            "alert": alert or {},
        },
        requested_by,
    )
    q("UPDATE investigations SET job_id=%s WHERE id=%s", (job_id, inv_id))
    audit(
        requested_by,
        actor_kind,
        "investigation.started",
        "investigation",
        inv_id,
        {"mmsi": mmsi, "trigger": trigger, "alert_id": alert_id, "job_id": job_id},
    )
    return {"investigation_id": inv_id, "job_id": job_id, "deduplicated": False}


# ---- evidence snapshots (ADR-0005): findings keep a copy of what they cite ----
def snapshot(
    entity_kind: str,
    entity_id: str,
    mmsi: int | None,
    kind: str,
    payload,
    source=None,
    reference=None,
    summary=None,
) -> None:
    q(
        """INSERT INTO evidence_snapshots (entity_kind, entity_id, mmsi, kind, source, reference, summary, payload)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            entity_kind,
            entity_id,
            mmsi,
            kind,
            source,
            reference,
            summary,
            Jsonb(payload),
        ),
    )


def positions_between(
    mmsi: int,
    start: str | None,
    end: str | None,
    hours_before: float = 1,
    hours_after: float = 1,
    cap: int = 2000,
) -> list[dict]:
    start = start or scenario_now()
    end = end or scenario_now()
    return q(
        """SELECT ts, ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon, sog, cog, nav_status, source FROM positions
           WHERE mmsi=%s AND ts BETWEEN %s::timestamptz - (%s::float * interval '1 hour') AND %s::timestamptz + (%s::float * interval '1 hour')
           ORDER BY ts LIMIT %s""",
        (mmsi, start, hours_before, end, hours_after, cap),
    )


def snapshot_alert(alert: dict, evidence: list[dict]) -> None:
    pts = positions_between(
        alert["mmsi"], alert.get("started_at"), alert.get("ended_at")
    )
    snapshot(
        "alert",
        alert["id"],
        alert["mmsi"],
        "positions",
        {"count": len(pts), "positions": pts},
        source="positions",
        summary=f"{len(pts)} reports around the alert window",
    )
    for e in evidence:
        snapshot(
            "alert",
            alert["id"],
            alert["mmsi"],
            "tool_output",
            e,
            source=e.get("source"),
            reference=e.get("reference"),
            summary=e.get("summary"),
        )


def snapshot_investigation(inv_id: str, report: dict) -> None:
    mmsi = report.get("mmsi")
    if not mmsi:
        return
    pts = positions_between(mmsi, None, None, hours_before=24, hours_after=0)
    snapshot(
        "investigation",
        inv_id,
        mmsi,
        "positions",
        {"count": len(pts), "positions": pts},
        source="positions",
        summary=f"{len(pts)} reports in the 24 h before the scenario clock",
    )
    reg = decrypting(
        lambda k: q(
            """SELECT mmsi, imo, name, flag, flag_history, registered_owner, operator, pgp_sym_decrypt(beneficial_owner_enc, %s) AS beneficial_owner, sanctions, fleet, notes
           FROM registry WHERE mmsi=%s""",
            (k, mmsi),
        )
    )
    if reg:
        snapshot(
            "investigation",
            inv_id,
            mmsi,
            "registry",
            reg[0],
            source="registry",
            summary="registry record at the time of the report",
        )
    net = q("SELECT ownership_network(%s, 2) AS net", (f"vessel:{mmsi}",))
    if net and net[0]["net"].get("found"):
        n = net[0]["net"]
        snapshot(
            "investigation",
            inv_id,
            mmsi,
            "ownership_network",
            n,
            source="ownership_network",
            summary=f"{len(n['nodes'])} nodes, {len(n['edges'])} edges at depth 2",
        )
    for e in report.get("evidence") or []:
        snapshot(
            "investigation",
            inv_id,
            mmsi,
            "tool_output",
            e,
            source=e.get("source"),
            reference=e.get("reference"),
            summary=e.get("summary"),
        )


# ---- models ----
class ReviewIn(BaseModel):
    decision: str  # accepted | rejected
    note: str | None = None


_KIND_ALIASES = {
    "mmsi_conflict": "mmsi_spoof",
    "mmsi_conflicts": "mmsi_spoof",
    "spoof": "mmsi_spoof",
    "gap": "ais_gap",
    "ais_gaps": "ais_gap",
    "loiter": "loitering",
    "incursion": "zone_incursion",
    "zone_incursions": "zone_incursion",
}


class AlertIn(BaseModel):
    mmsi: int
    kind: str
    severity: str
    score: float
    rationale: str
    started_at: str | None = None
    ended_at: str | None = None
    evidence: list[dict] = []
    created_by: str = "watch-agent"

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        k = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return _KIND_ALIASES.get(k, k)


class InvestigationIn(BaseModel):
    trigger: str = "manual"
    alert_id: str | None = None


class CompleteIn(BaseModel):
    report: dict
    trace_id: str | None = None
    manifest: dict | None = (
        None  # provenance (ADR-0006): models, prompts, tool versions, attempts
    )


class FailIn(BaseModel):
    error: str
    trace_id: str | None = None


# ---- routes ----
@app.get("/whoami")
def whoami(request: Request):
    """How the watch floor is signed in; the UI locks the officer field in oidc mode."""
    return {"officer": getattr(request.state, "officer", "") or "", "mode": auth_mode()}


@app.get("/health")
def health():
    q("SELECT 1")
    return {"status": "ok"}


@app.get("/vessels")
def vessels():
    """Latest position per vessel, dropping anything silent for more than a day so a replay
    scenario left in the table does not linger on a live plot (and vice versa)."""
    return q(
        """SELECT * FROM latest_positions
           WHERE ts > %s::timestamptz - interval '24 hours' ORDER BY mmsi""",
        (scenario_now(),),
    )


@app.get("/vessels/{mmsi}/track")
def track(mmsi: int, hours: float = 24):
    return q(
        """SELECT ts, ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon, sog, cog, nav_status FROM positions
           WHERE mmsi=%s AND ts > %s::timestamptz - (%s::float * interval '1 hour') ORDER BY ts""",
        (mmsi, scenario_now(), hours),
    )


@app.get("/zones")
def zones():
    rows = q(
        "SELECT id, name, kind, properties, ST_AsGeoJSON(geom::geometry)::json AS geometry FROM zones"
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": r["id"],
                "properties": {
                    "name": r["name"],
                    "kind": r["kind"],
                    **(r["properties"] or {}),
                },
                "geometry": r["geometry"],
            }
            for r in rows
        ],
    }


@app.get("/alerts")
def alerts(status: str | None = None):
    return q(
        "SELECT * FROM alerts WHERE (%s::text IS NULL OR status=%s) ORDER BY created_at DESC",
        (status, status),
    )


@app.post("/alerts", status_code=201)
def create_alert(a: AlertIn, request: Request):
    actor = agent_actor(request, a.created_by)
    rows = q(
        """INSERT INTO alerts (mmsi, kind, severity, score, started_at, ended_at, details, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            a.mmsi,
            a.kind,
            a.severity,
            a.score,
            a.started_at,
            a.ended_at,
            Jsonb({"rationale": a.rationale, "evidence": a.evidence}),
            actor,
        ),
    )
    audit(
        actor,
        "agent",
        "alert.raised",
        "alert",
        rows[0]["id"],
        {"mmsi": a.mmsi, "kind": a.kind, "severity": a.severity},
    )
    try:
        snapshot_alert(rows[0], a.evidence)
    except Exception as e:  # noqa: BLE001
        log.warning("evidence snapshot failed for alert %s: %s", rows[0]["id"], e)
    # Investigation policy: severe alerts open an investigation without waiting for a human.
    if a.severity in AUTO_INVESTIGATE:
        try:
            opened = open_investigation(a.mmsi, a.kind, rows[0], "policy", "system")
            if not opened["deduplicated"]:
                audit(
                    "policy",
                    "system",
                    "investigation.auto_opened",
                    "investigation",
                    opened["investigation_id"],
                    {"alert_id": rows[0]["id"], "severity": a.severity},
                )
        except Exception as e:  # noqa: BLE001
            log.warning("auto-investigation failed for alert %s: %s", rows[0]["id"], e)
    return rows[0]


@app.post("/alerts/{alert_id}/review")
def review_alert(
    alert_id: str,
    body: ReviewIn,
    officer_id: str = Depends(current_officer),
):
    """Watch officer disposition of an advisory finding: draft -> accepted | rejected."""
    if body.decision not in REVIEW_DECISIONS:
        raise HTTPException(400, "decision must be accepted or rejected")
    status_sql = ", status='dismissed'" if body.decision == "rejected" else ""
    rows = q(
        f"UPDATE alerts SET review_state=%s, reviewed_by=%s, reviewed_at=now(){status_sql} WHERE id=%s RETURNING *",
        (body.decision, officer_id, alert_id),
    )
    if not rows:
        raise HTTPException(404)
    audit(
        officer_id,
        "watch_officer",
        "alert.reviewed",
        "alert",
        alert_id,
        {"decision": body.decision, "note": body.note},
    )
    return rows[0]


@app.post("/alerts/{alert_id}/{action}")
def alert_action(
    alert_id: str, action: str, officer_id: str = Depends(current_officer)
):
    """Legacy verbs kept for the demo script: acknowledge = accepted, dismiss = rejected."""
    if action not in ("acknowledge", "dismiss"):
        raise HTTPException(400, "action must be acknowledge or dismiss")
    return review_alert(
        alert_id,
        ReviewIn(decision="accepted" if action == "acknowledge" else "rejected"),
        officer_id,
    )


@app.get("/investigations")
def investigations():
    return q(
        """SELECT i.id, i.mmsi, i.status, i.review_state, i.reviewed_by, i.reviewed_at, i.trigger, i.trace_id, i.created_at, i.updated_at, i.requested_by, i.job_id,
                  i.report->>'headline' AS headline, i.report->>'priority' AS priority,
                  j.status AS job_status, j.attempts, j.progress
           FROM investigations i LEFT JOIN jobs j ON j.id = i.job_id ORDER BY i.created_at DESC"""
    )


@app.get("/investigations/{inv_id}")
def investigation(inv_id: str):
    rows = q(
        """SELECT i.*, j.status AS job_status, j.attempts, j.error AS job_error, j.progress
           FROM investigations i LEFT JOIN jobs j ON j.id = i.job_id WHERE i.id=%s""",
        (inv_id,),
    )
    if not rows:
        raise HTTPException(404)
    row = rows[0]
    cost, tokens = manifest_cost(row.get("manifest"))
    row["cost_usd"], row["tokens"] = cost, tokens
    return row


@app.post("/investigations/{mmsi}", status_code=202)
def start_investigation(
    mmsi: int,
    body: InvestigationIn,
    officer_id: str = Depends(current_officer),
):
    """Queue an investigation. Progress arrives on /events; the row is polled at /investigations/{id}."""
    alert = (
        q("SELECT * FROM alerts WHERE id=%s", (body.alert_id,))[0]
        if body.alert_id
        else None
    )
    opened = open_investigation(mmsi, body.trigger, alert, officer_id, "watch_officer")
    return {**opened, "status": "queued"}


@app.post("/investigations/{inv_id}/complete")
def complete(inv_id: str, body: CompleteIn, request: Request):
    actor = agent_actor(request, "orchestrator-agent")
    q(
        "UPDATE investigations SET status='complete', report=%s, trace_id=%s, manifest=%s, updated_at=now() WHERE id=%s",
        (Jsonb(body.report), body.trace_id, Jsonb(body.manifest or {}), inv_id),
    )
    q(
        "UPDATE alerts SET status='investigated' WHERE mmsi=%s AND status='open'",
        (body.report.get("mmsi"),),
    )
    audit(
        actor,
        "agent",
        "investigation.completed",
        "investigation",
        inv_id,
        {
            "priority": body.report.get("priority"),
            "confidence": body.report.get("confidence"),
        },
        body.trace_id,
    )
    run_metrics.publish(body.manifest or {})
    try:
        snapshot_investigation(inv_id, body.report)
        ids = [
            str(r["id"])
            for r in q(
                "SELECT id FROM evidence_snapshots WHERE entity_kind='investigation' AND entity_id=%s",
                (inv_id,),
            )
        ]
        q(
            "UPDATE investigations SET manifest = coalesce(manifest, '{}'::jsonb) || %s::jsonb WHERE id=%s",
            (Jsonb({"evidence_snapshots": ids}), inv_id),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("evidence snapshot failed for investigation %s: %s", inv_id, e)
    return {"ok": True}


@app.get("/evidence")
def evidence(entity_kind: str, entity_id: str):
    """What a finding cited, as captured at the time (positions, registry, network, tool outputs)."""
    return q(
        """SELECT id, entity_kind, entity_id, mmsi, kind, source, reference, summary, captured_at,
                  CASE WHEN kind='positions' THEN (payload->>'count')::int ELSE NULL END AS position_count
           FROM evidence_snapshots WHERE entity_kind=%s AND entity_id=%s ORDER BY captured_at, kind""",
        (entity_kind, entity_id),
    )


@app.get("/evidence/{snapshot_id}")
def evidence_item(snapshot_id: str):
    rows = q("SELECT * FROM evidence_snapshots WHERE id=%s", (snapshot_id,))
    if not rows:
        raise HTTPException(404)
    return rows[0]


@app.get("/network/{mmsi}")
def network(mmsi: int, depth: int = 2):
    """Ownership network for the UI (person names stay pseudonymous here; the Investigator tool decrypts)."""
    return q(
        "SELECT ownership_network(%s, %s) AS net",
        (f"vessel:{mmsi}", max(1, min(depth, 4))),
    )[0]["net"]


@app.post("/investigations/{inv_id}/fail")
def fail(inv_id: str, body: FailIn, request: Request):
    actor = agent_actor(request, "orchestrator-agent")
    q(
        "UPDATE investigations SET status='failed', report=%s, trace_id=%s, updated_at=now() WHERE id=%s",
        (Jsonb({"error": body.error}), body.trace_id, inv_id),
    )
    audit(
        actor,
        "agent",
        "investigation.failed",
        "investigation",
        inv_id,
        {"error": body.error[:500]},
        body.trace_id,
    )
    return {"ok": True}


class ProgressIn(BaseModel):
    step: str
    status: str = "started"  # started | finished | failed
    detail: str = ""


@app.post("/investigations/{inv_id}/progress")
def investigation_progress(inv_id: str, body: ProgressIn, request: Request):
    """Node-level progress reported by the orchestrator (specialist called, report drafted)."""
    rows = q("SELECT job_id, mmsi FROM investigations WHERE id=%s", (inv_id,))
    if not rows or not rows[0]["job_id"]:
        raise HTTPException(404, "no job for that investigation")
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "step": body.step,
        "status": body.status,
        "detail": body.detail[:500],
    }
    q(
        "UPDATE jobs SET progress = progress || %s::jsonb, updated_at = now() WHERE id=%s",
        (Jsonb([entry]), rows[0]["job_id"]),
    )
    events.publish(
        type="job",
        job_id=rows[0]["job_id"],
        kind="investigation",
        investigation_id=inv_id,
        mmsi=rows[0]["mmsi"],
        actor=agent_actor(request, "orchestrator-agent"),
        **entry,
    )
    return {"ok": True}


@app.post("/investigations/{inv_id}/review")
def review_investigation(
    inv_id: str, body: ReviewIn, officer_id: str = Depends(current_officer)
):
    """Watch officer disposition of a VOI report: draft -> accepted | rejected."""
    if body.decision not in REVIEW_DECISIONS:
        raise HTTPException(400, "decision must be accepted or rejected")
    rows = q(
        "UPDATE investigations SET review_state=%s, reviewed_by=%s, reviewed_at=now() WHERE id=%s AND status='complete' RETURNING id, review_state, reviewed_by, reviewed_at, trace_id",
        (body.decision, officer_id, inv_id),
    )
    if not rows:
        raise HTTPException(404, "no completed investigation with that id")
    audit(
        officer_id,
        "watch_officer",
        "investigation.reviewed",
        "investigation",
        inv_id,
        {"decision": body.decision, "note": body.note},
    )
    # The officer's verdict becomes a CloudWatch metric next to the evaluation scores.
    review_metrics.publish(
        body.decision, officer_id, (rows[0].get("report") or {}).get("priority")
    )
    return {k: v for k, v in rows[0].items() if k != "trace_id"}


@app.post("/sweep", status_code=202)
def sweep(hours: float = 12, officer_id: str = Depends(current_officer)):
    """Queue a watch sweep. The worker calls the Watch agent directly."""
    job_id, created = enqueue(
        "sweep",
        sweep_key(datetime.now(UTC), 0, hours),
        {"hours": hours},
        officer_id,
    )
    audit(
        officer_id,
        "watch_officer",
        "sweep.requested",
        "sweep",
        job_id,
        {"hours": hours},
    )
    return {
        "status": "queued" if created else "already_queued",
        "job_id": job_id,
        "hours": hours,
    }


@app.get("/jobs")
def jobs(status: str | None = None, limit: int = 50):
    return q(
        "SELECT id, kind, status, attempts, max_attempts, requested_by, created_at, started_at, finished_at, error, payload->>'mmsi' AS mmsi, payload->>'investigation_id' AS investigation_id FROM jobs WHERE (%s::text IS NULL OR status=%s) ORDER BY created_at DESC LIMIT %s",
        (status, status, min(max(limit, 1), 500)),
    )


@app.get("/jobs/{job_id}")
def job(job_id: str):
    rows = q("SELECT * FROM jobs WHERE id=%s", (job_id,))
    if not rows:
        raise HTTPException(404)
    return rows[0]


@app.get("/events")
async def event_stream():
    """Server-sent job progress events (queued, node started/finished, succeeded, failed)."""
    r = redis.from_url(REDIS_URL)

    async def gen():
        last = "$"
        while True:
            res = await r.xread({EVENTS_STREAM: last}, block=15000, count=100)
            if not res:
                yield {"event": "ping", "data": "{}"}
                continue
            for _, entries in res:
                for eid, fields in entries:
                    last = eid
                    yield {"event": "job", "data": fields[b"data"].decode()}
            await asyncio.sleep(0)

    return EventSourceResponse(gen())


# ---- evals and SLOs (phase 5) ----
class EvalRunIn(BaseModel):
    suite: str
    scenario: str | None = None
    scores: dict
    passed: bool | None = None
    thresholds: dict = {}
    code_revision: str | None = None
    details: dict = {}


@app.post("/evals", status_code=201)
def record_eval(body: EvalRunIn):
    rows = q(
        "INSERT INTO eval_runs (suite, scenario, scores, passed, thresholds, code_revision, details) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, ts",
        (
            body.suite,
            body.scenario,
            Jsonb(body.scores),
            body.passed,
            Jsonb(body.thresholds),
            body.code_revision,
            Jsonb(body.details),
        ),
    )
    audit(
        "evals",
        "system",
        "eval.recorded",
        "eval_run",
        rows[0]["id"],
        {"suite": body.suite, "passed": body.passed, "scores": body.scores},
    )
    return rows[0]


@app.get("/evals")
def list_evals(suite: str | None = None, limit: int = 50):
    return q(
        "SELECT id, ts, suite, scenario, scores, passed, code_revision FROM eval_runs WHERE (%s::text IS NULL OR suite=%s) ORDER BY ts DESC LIMIT %s",
        (suite, suite, min(max(limit, 1), 500)),
    )


# Bedrock on-demand list prices, USD per million tokens (input, output). Override with MODEL_PRICES_JSON.
DEFAULT_PRICES = {
    "nova-micro": (0.035, 0.14),
    "nova-lite": (0.06, 0.24),
    "nova-2-lite": (0.30, 2.50),
    "nova-pro": (0.80, 3.20),
    "nova-premier": (2.50, 12.50),
    "llama4-maverick": (0.24, 0.97),
    "llama3-3-70b": (0.72, 0.72),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}
PRICES = {
    **DEFAULT_PRICES,
    **{
        k: tuple(v) for k, v in json.loads(os.getenv("MODEL_PRICES_JSON", "{}")).items()
    },
}


def usage_cost(model_id: str | None, usage: dict | None) -> float | None:
    """USD for one node's token usage, or None when the model is unpriced."""
    if not model_id or not usage:
        return None
    price = next((p for k, p in PRICES.items() if k in model_id), None)
    if not price:
        return None
    return round(
        usage.get("inputTokens", 0) / 1e6 * price[0]
        + usage.get("outputTokens", 0) / 1e6 * price[1],
        5,
    )


def manifest_cost(manifest: dict | None) -> tuple[float | None, dict]:
    """Cost and tokens across the nodes recorded in a provenance manifest."""
    if not manifest:
        return None, {}
    total, tokens_in, tokens_out, priced = 0.0, 0, 0, False
    for n in manifest.get("nodes") or []:
        u = n.get("usage") or {}
        tokens_in += u.get("inputTokens", 0)
        tokens_out += u.get("outputTokens", 0)
        c = usage_cost(n.get("model_id"), u)
        if c is not None:
            total += c
            priced = True
    return (round(total, 4) if priced else None), {
        "inputTokens": tokens_in,
        "outputTokens": tokens_out,
    }


def slo_snapshot() -> dict:
    """Service-level indicators from the database: latencies, completion, lag, cost, eval recall."""
    sweeps = q(
        """SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) AS p95,
                  count(*) FILTER (WHERE status='succeeded') AS ok, count(*) AS total
           FROM jobs WHERE kind='sweep' AND finished_at IS NOT NULL AND created_at > now() - interval '7 days'"""
    )[0]
    inv = q(
        """SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) AS p95,
                  count(*) FILTER (WHERE status='succeeded') AS ok, count(*) FILTER (WHERE status IN ('succeeded','failed','dead')) AS total
           FROM jobs WHERE kind='investigation' AND created_at > now() - interval '7 days'"""
    )[0]
    lag = q(
        """SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM i.created_at - a.created_at)) AS p95
           FROM investigations i JOIN alerts a ON a.id = i.alert_id WHERE i.created_at > now() - interval '7 days'"""
    )[0]
    recent = q(
        "SELECT manifest FROM investigations WHERE status='complete' AND created_at > now() - interval '30 days' ORDER BY created_at DESC LIMIT 200"
    )
    costs = [
        c for c, _ in (manifest_cost(r["manifest"]) for r in recent) if c is not None
    ]
    today = q(
        "SELECT manifest FROM investigations WHERE status='complete' AND created_at > date_trunc('day', now())"
    )
    cost_today = sum(
        c for c, _ in (manifest_cost(r["manifest"]) for r in today) if c is not None
    )
    ev = q(
        "SELECT suite, scores, passed, ts FROM eval_runs WHERE suite IN ('watch','e2e') ORDER BY ts DESC LIMIT 1"
    )
    return {
        "window": "7d",
        "sweep_latency_p95_s": sweeps["p95"],
        "sweep_completion_ratio": (sweeps["ok"] / sweeps["total"])
        if sweeps["total"]
        else None,
        "investigation_latency_p95_s": inv["p95"],
        "investigation_completion_ratio": (inv["ok"] / inv["total"])
        if inv["total"]
        else None,
        "alert_to_investigation_lag_p95_s": lag["p95"],
        "investigation_cost_usd_avg": (
            round(sum(costs) / len(costs), 4) if costs else None
        ),
        "investigation_cost_usd_today": round(cost_today, 4),
        "investigations_priced": len(costs),
        "eval_recall_latest": (ev[0]["scores"].get("recall") if ev else None),
        "eval_latest_ts": (ev[0]["ts"] if ev else None),
        "targets": SLO_TARGETS,
    }


SLO_TARGETS = {
    "sweep_latency_p95_s": 300,
    "investigation_latency_p95_s": 600,
    "investigation_completion_ratio": 0.9,
    "alert_to_investigation_lag_p95_s": 120,
    "investigation_cost_usd_avg": float(
        os.getenv("SLO_MAX_COST_PER_INVESTIGATION", "2.0")
    ),
    "eval_recall_latest": float(os.getenv("SLO_MIN_EVAL_RECALL", "0.8")),
}


@app.get("/slo")
def slo():
    return slo_snapshot()


@app.get("/metrics")
def metrics():
    """Prometheus exposition of the SLIs (scraped locally by Prometheus, on AWS by the ADOT collector)."""
    from fastapi.responses import PlainTextResponse

    s = slo_snapshot()
    lines = []
    for k, v in s.items():
        if k in ("window", "targets", "eval_latest_ts") or v is None:
            continue
        lines.append(f"# TYPE argus_{k} gauge")
        lines.append(f"argus_{k} {float(v)}")
    for k, v in SLO_TARGETS.items():
        lines.append(f"argus_target_{k} {float(v)}")
    counts = q(
        "SELECT kind, status, count(*) AS n FROM jobs WHERE created_at > now() - interval '1 day' GROUP BY 1,2"
    )
    lines.append("# TYPE argus_jobs_24h gauge")
    for r in counts:
        lines.append(
            f'argus_jobs_24h{{kind="{r["kind"]}",status="{r["status"]}"}} {r["n"]}'
        )
    open_alerts = q(
        "SELECT count(*) AS n FROM alerts WHERE status='open' AND review_state='draft'"
    )[0]["n"]
    lines.append(f"argus_alerts_awaiting_review {open_alerts}")
    lines.append("# TYPE argus_alerts_open_by_severity gauge")
    for r in q(
        "SELECT severity, count(*) AS n FROM alerts WHERE status='open' AND review_state='draft' GROUP BY 1"
    ):
        lines.append(
            f'argus_alerts_open_by_severity{{severity="{r["severity"]}"}} {r["n"]}'
        )
    oldest = q(
        "SELECT extract(epoch FROM now() - min(created_at)) AS s FROM alerts WHERE status='open' AND review_state='draft'"
    )[0]["s"]
    lines.append(f"argus_oldest_unreviewed_alert_s {float(oldest or 0)}")
    lines.append("# TYPE argus_investigations_by_status gauge")
    for r in q(
        "SELECT status, count(*) AS n FROM investigations WHERE created_at > now() - interval '1 day' GROUP BY 1"
    ):
        lines.append(
            f'argus_investigations_by_status{{status="{r["status"]}"}} {r["n"]}'
        )
    proposed = q("SELECT count(*) AS n FROM tasking_requests WHERE status='proposed'")[
        0
    ]["n"]
    lines.append(f"argus_tasking_proposed {proposed}")
    feed = q(
        "SELECT extract(epoch FROM now() - max(ts)) AS lag, count(DISTINCT mmsi) FILTER (WHERE ts > now() - interval '10 minutes') AS vessels FROM positions WHERE ts > now() - interval '1 day'"
    )[0]
    lines.append(f"argus_feed_lag_s {float(feed['lag'] or 0)}")
    lines.append(f"argus_vessels_reporting_10m {int(feed['vessels'] or 0)}")
    stats = sli_pure.node_stats(
        [
            r["manifest"]
            for r in q(
                "SELECT manifest FROM investigations WHERE manifest IS NOT NULL AND created_at > now() - interval '1 day'"
            )
        ]
    )
    lines.extend(sli_pure.prometheus_lines(stats))
    lines.append("# TYPE argus_eval_score gauge")
    for r in q(
        "SELECT DISTINCT ON (suite) suite, scores, passed FROM eval_runs ORDER BY suite, ts DESC"
    ):
        for k, v in (r["scores"] or {}).items():
            if isinstance(v, int | float):
                lines.append(
                    f'argus_eval_score{{suite="{r["suite"]}",metric="{k}"}} {float(v)}'
                )
        lines.append(
            f'argus_eval_passed{{suite="{r["suite"]}"}} {1 if r["passed"] else 0}'
        )
    lines.append(
        f'argus_build_info{{code_revision="{os.getenv("GIT_SHA", "unknown")}",ais_mode="{os.getenv("AIS_MODE", "replay")}"}} 1'
    )
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/audit")
def audit_log(
    limit: int = 100, entity_kind: str | None = None, entity_id: str | None = None
):
    """Recent audit events, newest first. The table is append-only."""
    return q(
        """SELECT * FROM audit_events
           WHERE (%s::text IS NULL OR entity_kind=%s) AND (%s::text IS NULL OR entity_id=%s)
           ORDER BY ts DESC LIMIT %s""",
        (entity_kind, entity_kind, entity_id, entity_id, min(max(limit, 1), 1000)),
    )


@app.get("/tasking")
def tasking():
    return q(
        "SELECT id, mmsi, sensor, priority, status, rationale, window_start, window_end, created_by, created_at, decided_by, decided_at, ST_AsGeoJSON(aoi::geometry)::json AS aoi FROM tasking_requests ORDER BY created_at DESC"
    )


@app.post("/tasking/{task_id}/{decision}")
def decide_tasking(
    task_id: str, decision: str, officer_id: str = Depends(current_officer)
):
    """Human approval step. Agents only ever create 'proposed' requests."""
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    status = "approved" if decision == "approve" else "rejected"
    rows = q(
        "UPDATE tasking_requests SET status=%s, decided_by=%s, decided_at=now() WHERE id=%s AND status='proposed' RETURNING id, status, decided_by, decided_at",
        (status, officer_id, task_id),
    )
    if not rows:
        raise HTTPException(404, "no proposed tasking request with that id")
    audit(
        officer_id,
        "watch_officer",
        f"tasking.{status}",
        "tasking_request",
        task_id,
    )
    return rows[0]


@app.get("/stream")
async def stream():
    """Server-sent events of live AIS positions from the Redis stream (for the map)."""
    r = redis.from_url(REDIS_URL)

    async def gen():
        last = "$"
        while True:
            res = await r.xread({"ais:positions": last}, block=5000, count=200)
            if not res:
                yield {"event": "ping", "data": "{}"}
                continue
            for _, entries in res:
                for eid, fields in entries:
                    last = eid
                    yield {"event": "position", "data": fields[b"data"].decode()}
            await asyncio.sleep(0)

    return EventSourceResponse(gen())


def scenario_meta(key: str) -> Any | None:
    """Scenario metadata published by the replay task: the scenario_meta table (shared on
    AWS), falling back to the /app/shared volume for a local stack on an older schema."""
    try:
        rows = q("SELECT value FROM scenario_meta WHERE key=%s", (key,))
        if rows:
            return rows[0]["value"]
    except Exception as e:  # table may not exist yet on an old local volume
        log.warning("scenario_meta unavailable: %s", e)
    try:
        with open(f"/app/shared/{key}.json") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


@app.get("/area")
def area():
    """The watched area of interest plus feed status, so the UI can say what is being watched
    and whether positions are still arriving."""
    meta = scenario_meta("area")
    if meta is None:
        ext = q(
            "SELECT ST_XMin(e) min_lon, ST_YMin(e) min_lat, ST_XMax(e) max_lon, ST_YMax(e) max_lat FROM (SELECT ST_Extent(geom::geometry) e FROM positions) s"
        )
        meta = {
            "name": "positions extent",
            "bbox": ext[0] if ext else None,
            "mode": "unknown",
        }
    feed = q(
        "SELECT max(ts) AS latest_ts, count(*) FILTER (WHERE ts > (SELECT max(ts) FROM positions) - interval '10 minutes') AS recent, count(DISTINCT mmsi) AS vessels FROM positions"
    )
    return {**meta, "scenario_end": scenario_now(), "feed": feed[0] if feed else {}}


@app.get("/ground-truth")
def ground_truth():
    """Injected anomalies from the scenario (for the evaluation page). Not visible to agents."""
    truth = scenario_meta("ground_truth")
    return [] if truth is None else truth
