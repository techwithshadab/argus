"""Platform API: vessels, tracks, alerts, investigations, tasking approvals, live stream."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import redis.asyncio as redis
import review_metrics
import run_metrics
import sli_pure
import sweep_metrics
from callerauth import CallerAuthMiddleware, matches_route, officer_roles
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
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="Argus API", version="0.1.0")
#: Origins allowed to script the API from a browser. The watch floor is served from the
#: same origin as `/api/*` (nginx proxies it), so nothing needs this by default: a
#: wildcard let any page on the internet read every route the balancer let it reach.
#: Set CORS_ALLOW_ORIGINS to a comma list for a separately served development page.
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-watch-officer"],
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
    # The orchestrator reads the track to derive where the vessel went dark
    # (`gap_position`), because the Tasking agent has no AIS tool and, handed only
    # prose, once proposed a SAR collection over New York for a vessel off Singapore.
    # Without this the call is refused, the AOI check has nothing to compare against
    # and every tasking recommendation is recorded unverified: 88 rejections an hour.
    ("GET", "/vessels/*/track"),
)


def is_agent_route(path: str, method: str) -> bool:
    return matches_route(_AGENT_ROUTES, path, method)


def requires_iam(path: str, method: str) -> bool:
    return needs_iam(path, method, is_agent_route)


app.add_middleware(
    CallerAuthMiddleware, protected=requires_iam, agent_route=is_agent_route
)
app.add_middleware(OfficerAuthMiddleware)  # outermost: decides before caller auth


def current_officer(request: Request) -> str:
    """The identity recorded on a decision (see officerauth.officer_of)."""
    try:
        return officer_of(request.state)
    except AuthError as e:
        raise HTTPException(e.status, e.message) from e


pool = RotatingPool(min_size=1, max_size=10)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
#: The longest blocking XREAD the SSE handlers issue, and a socket read deadline kept
#: above it so the deadline itself never interrupts a legitimate block.
#: What actually killed these streams (~1700 errors/hour, and the watch floor's progress
#: panel freezing so a running investigation looked stuck) is the server closing an idle
#: connection mid-block: measured against a real Redis, that raises whatever the timeout
#: is set to, and the old generators let it escape and end the stream. The retry in
#: `gen()` is the fix; this deadline only bounds a hung socket.
SSE_BLOCK_MS = 15000
SSE_TIMEOUT_S = 30


def sse_redis():
    """A client for one SSE stream. Per request on purpose: each generator blocks in
    `xread`, so a shared connection would serialise the streams behind each other. The
    generator closes it in its `finally` (P17)."""
    return redis.from_url(
        REDIS_URL,
        socket_timeout=SSE_TIMEOUT_S,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
    )


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


def jsonable(obj: Any) -> Any:
    """`json.dumps` default for values that came out of psycopg.

    `jobs.id` and friends arrive as `UUID` objects and timestamps as `datetime`, and
    both raise `TypeError: Object of type UUID is not JSON serializable` — inside
    `Jsonb(...)` that means an audit write blows up *after* the state change it records,
    which is how `open_investigation` committed its rows and then returned 500.
    """
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    return str(obj)


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, default=jsonable)


def jsonb(value: Any) -> Jsonb:
    """Jsonb that never raises on a UUID or a datetime (see jsonable)."""
    return Jsonb(value, dumps=_dump_json)


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
    """Append-only audit event (see data/sql/002_review_audit.sql).

    Raises if the write fails, and every caller lets it: an unaudited state change is
    not an acceptable outcome on this API (CLAUDE.md). The docstring used to claim the
    opposite, which is how a caller would come to treat it as best effort (P19). The
    table is append-only by trigger; never update or delete a row here.
    """
    q(
        """INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details, trace_id, manifest_ref)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            actor,
            actor_kind,
            action,
            entity_kind,
            entity_id,
            jsonb(details or {}),
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
        (kind, key, jsonb(payload), DEFAULT_TIMEOUTS[kind], requested_by),
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
    """Create the investigation row and its job. Returns {investigation_id, job_id, deduplicated}.

    The job row and the investigation row are inserted in one transaction, so the
    partial unique index on the job's idempotency key arbitrates. A read-then-write
    let the officer's request and the auto-investigate path both see no active job,
    both insert an investigation, and both attach to the winner's job: the loser was
    left `running` forever, counted as load by the KPI strip and never reaped (P8).
    """
    alert_id = (alert or {}).get("id")
    key = investigation_key(mmsi, trigger, alert_id, datetime.now(UTC))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO investigations (mmsi, trigger, alert_id, requested_by)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (mmsi, trigger, alert_id, requested_by),
        )
        inv_id = cur.fetchone()["id"]
        cur.execute(
            """INSERT INTO jobs (kind, idempotency_key, payload, timeout_s, requested_by)
               VALUES ('investigation',%s,%s,%s,%s)
               ON CONFLICT (kind, idempotency_key) WHERE status IN ('queued', 'running')
               DO NOTHING RETURNING id""",
            (
                key,
                jsonb(
                    {
                        "mmsi": mmsi,
                        "trigger": trigger,
                        "investigation_id": str(inv_id),
                        "alert": alert or {},
                    }
                ),
                DEFAULT_TIMEOUTS["investigation"],
                requested_by,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # Another caller won the key. Roll the investigation row back with it, so
            # no orphan is left, and return theirs.
            conn.rollback()
            active = q(
                """SELECT id FROM jobs WHERE kind='investigation' AND idempotency_key=%s
                    AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1""",
                (key,),
            )
            job_id = active[0]["id"] if active else None
            inv = (
                q("SELECT id FROM investigations WHERE job_id=%s", (job_id,))
                if job_id
                else []
            )
            return {
                "investigation_id": inv[0]["id"] if inv else None,
                "job_id": job_id,
                "deduplicated": True,
            }
        job_id = row["id"]
        cur.execute("UPDATE investigations SET job_id=%s WHERE id=%s", (job_id, inv_id))
        conn.commit()
    # Only after the rows are committed, so a worker can never claim a job whose
    # investigation does not exist yet.
    queues["investigation"].send(job_id)
    events.publish(
        type="job",
        job_id=job_id,
        kind="investigation",
        step="job",
        status="queued",
        investigation_id=str(inv_id),
        mmsi=mmsi,
    )
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
            jsonb(payload),
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
    """Freeze what this investigation cited. Idempotent: delivery is at least once, and
    the snapshot now runs before the status guard, so a redelivered completion would
    otherwise write a second set of rows (P18)."""
    mmsi = report.get("mmsi")
    if not mmsi:
        return
    q(
        "DELETE FROM evidence_snapshots WHERE entity_kind='investigation' AND entity_id=%s",
        (inv_id,),
    )
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
    # The snapshot is served to officers through GET /evidence, so it must stay
    # pseudonymous: record whether a beneficial owner is on file, never the name
    # itself. Only the registry MCP tool decrypts that column (ADR-0019).
    reg = q(
        """SELECT mmsi, imo, name, flag, flag_history, registered_owner, operator,
                  (beneficial_owner_enc IS NOT NULL) AS beneficial_owner_on_file,
                  sanctions, fleet, notes
           FROM registry WHERE mmsi=%s""",
        (mmsi,),
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


#: The anomaly kinds this platform stores, and the severities it accepts. Both were
#: free text: a model answering "HIGH — definitely" was stored verbatim, skipped the
#: auto-investigate policy that compares against "high", and was interpolated
#: unescaped into the Prometheus exposition (P10).
ALERT_KINDS = ("ais_gap", "mmsi_spoof", "loitering", "zone_incursion", "rendezvous")
ALERT_SEVERITIES = ("low", "medium", "high")

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
    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=4000)
    started_at: str | None = None
    ended_at: str | None = None
    evidence: list[dict] = []
    created_by: str = Field(default="watch-agent", max_length=100)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        k = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        k = _KIND_ALIASES.get(k, k)
        # Aliases are mapped first: models echo detector names (`mmsi_conflict`).
        # Anything still unrecognised is refused rather than stored (P10).
        if k not in ALERT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(ALERT_KINDS)}")
        return k

    @field_validator("severity")
    @classmethod
    def _severity(cls, v: str) -> str:
        sev = (v or "").strip().lower()
        if sev not in ALERT_SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(ALERT_SEVERITIES)}")
        return sev

    @field_validator("started_at", "ended_at")
    @classmethod
    def _timestamp(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        try:
            # A malformed value used to reach psycopg and 500 an agent route, losing
            # the alert a whole sweep had earned.
            datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError("must be an ISO 8601 timestamp") from e
        return str(v)


#: Why an investigation was opened. Free text used to reach the job's idempotency key,
#: so any distinct string defeated the one-active-job index and a caller could queue
#: unbounded concurrent investigations for one vessel (P7). The alert kinds are here
#: because `create_alert` opens an investigation with the alert's kind as the trigger.
TRIGGERS = ("manual", "policy", *ALERT_KINDS)


class InvestigationIn(BaseModel):
    trigger: str = "manual"
    alert_id: str | None = None

    @field_validator("trigger")
    @classmethod
    def _trigger(cls, v: str) -> str:
        t = (v or "").strip().lower()
        t = _KIND_ALIASES.get(t, t)
        if t not in TRIGGERS:
            raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}")
        return t

    @field_validator("alert_id")
    @classmethod
    def _alert_id(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        try:
            return str(UUID(str(v)))
        except ValueError as e:
            # An unparseable id used to reach psycopg and come back as a 500.
            raise ValueError("alert_id must be a UUID") from e
        return None


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


#: A week is the most track the map or a specialist ever needs. Uncapped, one request
#: scanned every daily partition of `positions` for that vessel (P9).
TRACK_MAX_HOURS = 168
TRACK_MAX_POINTS = 20000
#: Manual sweeps share a key within this many minutes.
MANUAL_SWEEP_BUCKET_MIN = 1
#: Progress entries kept per job. A node reports a handful; an agent that loops
#: grew the array without limit and every list page re-serialised it (P9).
PROGRESS_MAX_ENTRIES = 200


@app.get("/vessels/{mmsi}/track")
def track(mmsi: int, hours: float = 24):
    hours = max(0.0, min(float(hours), TRACK_MAX_HOURS))
    return q(
        """SELECT ts, ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon, sog, cog, nav_status FROM positions
           WHERE mmsi=%s AND ts > %s::timestamptz - (%s::float * interval '1 hour')
           ORDER BY ts LIMIT %s""",
        (mmsi, scenario_now(), hours, TRACK_MAX_POINTS),
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


#: Rows any list route will return at once. The watch floor shows the newest first and
#: an officer works the top of the queue; an unbounded select of a growing table was a
#: slow page and a large response for no benefit (P5).
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 1000


def list_limit(limit: int | None) -> int:
    """The row cap for a list route. `None` means the default; a number is clamped."""
    if limit is None:
        return LIST_LIMIT_DEFAULT
    return max(1, min(int(limit), LIST_LIMIT_MAX))


@app.get("/alerts")
def alerts(status: str | None = None, limit: int | None = None, offset: int = 0):
    return q(
        """SELECT * FROM alerts WHERE (%s::text IS NULL OR status=%s)
           ORDER BY created_at DESC LIMIT %s OFFSET %s""",
        (status, status, list_limit(limit), max(0, offset)),
    )


@app.post("/alerts", status_code=201)
def create_alert(a: AlertIn, request: Request):
    actor = agent_actor(request, a.created_by)
    # The same vessel, kind and window is one alert however many sweeps see it. The
    # unique index (data/sql/010) decides rather than a read-then-write, because two
    # sweeps can race; a duplicate returns the alert that already exists (P5).
    rows = q(
        """INSERT INTO alerts (mmsi, kind, severity, score, started_at, ended_at, details, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (mmsi, kind, started_at)
             WHERE status <> 'dismissed' AND started_at IS NOT NULL
           DO NOTHING
           RETURNING *""",
        (
            a.mmsi,
            a.kind,
            a.severity,
            a.score,
            a.started_at,
            a.ended_at,
            jsonb({"rationale": a.rationale, "evidence": a.evidence}),
            actor,
        ),
    )
    if not rows:
        existing = q(
            """SELECT * FROM alerts
                WHERE mmsi=%s AND kind=%s AND started_at=%s AND status <> 'dismissed'
                ORDER BY created_at DESC LIMIT 1""",
            (a.mmsi, a.kind, a.started_at),
        )
        if existing:
            log.info("alert for %s %s already raised", a.mmsi, a.kind)
            sweep_metrics.publish(raised=0, duplicate=1)
            return existing[0]
        raise HTTPException(409, "alert could not be stored")
    sweep_metrics.publish(raised=1, duplicate=0)
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
    # A review is a one-way transition out of draft. Without the guard a second
    # call flipped a colleague's decision and wrote a second audit entry as if it
    # were the first, so the trail no longer showed who decided the case.
    rows = q(
        f"UPDATE alerts SET review_state=%s, reviewed_by=%s, reviewed_at=now(){status_sql} "
        "WHERE id=%s AND review_state='draft' RETURNING *",
        (body.decision, officer_id, alert_id),
    )
    if not rows:
        exists = q("SELECT 1 FROM alerts WHERE id=%s", (alert_id,))
        raise HTTPException(
            409 if exists else 404,
            "alert already reviewed" if exists else "no alert with that id",
        )
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
    alert = None
    if body.alert_id:
        rows = q("SELECT * FROM alerts WHERE id=%s", (body.alert_id,))
        if not rows:
            raise HTTPException(404, "no alert with that id")
        alert = rows[0]
        # The alert names the vessel. Without this check a case was opened on one
        # vessel carrying another vessel's window and evidence (P7).
        if int(alert["mmsi"]) != mmsi:
            raise HTTPException(
                400,
                f"alert {body.alert_id} is for MMSI {alert['mmsi']}, not {mmsi}",
            )
    opened = open_investigation(mmsi, body.trigger, alert, officer_id, "watch_officer")
    return {**opened, "status": "queued"}


@app.post("/investigations/{inv_id}/complete")
def complete(inv_id: str, body: CompleteIn, request: Request):
    """Record a finished report. Only a running investigation may be completed.

    Delivery is at least once and a slow branch can return after the case moved on,
    so the guard is in the UPDATE itself: without it a replayed call overwrote an
    already reviewed report, and the officer's decision silently disappeared.
    """
    actor = agent_actor(request, "orchestrator-agent")
    # The snapshot is the case's evidence (ADR-0005). Capture it before the status
    # moves: a failure used to be logged and nothing else, leaving a case that reads
    # complete and reviewable with nothing behind it, and P4's one-way guard means it
    # can never be repaired afterwards (P18).
    running = q(
        "SELECT 1 FROM investigations WHERE id=%s AND status='running'", (inv_id,)
    )
    if not running:
        raise HTTPException(409, "investigation is not running")
    try:
        snapshot_investigation(inv_id, body.report)
        snapshot_ids = [
            str(r["id"])
            for r in q(
                "SELECT id FROM evidence_snapshots WHERE entity_kind='investigation' AND entity_id=%s",
                (inv_id,),
            )
        ]
    except Exception as e:  # noqa: BLE001
        log.exception("evidence snapshot failed for investigation %s", inv_id)
        raise HTTPException(
            503, "evidence snapshot failed; the report was not stored"
        ) from e
    done = q(
        """UPDATE investigations SET status='complete', report=%s, trace_id=%s,
                  manifest=%s, updated_at=now()
            WHERE id=%s AND status='running' RETURNING mmsi""",
        (
            jsonb(body.report),
            body.trace_id,
            jsonb({**(body.manifest or {}), "evidence_snapshots": snapshot_ids}),
            inv_id,
        ),
    )
    if not done:
        raise HTTPException(409, "investigation is not running")
    # The row's own MMSI, never the report's: a report that names another vessel
    # would otherwise close that vessel's open alerts.
    q(
        "UPDATE alerts SET status='investigated' WHERE mmsi=%s AND status='open'",
        (done[0]["mmsi"],),
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


#: Payload keys that must never leave the API, whatever wrote the snapshot (P2).
PERSONAL_SNAPSHOT_KEYS = ("beneficial_owner", "person_name", "owner_name")


def redact_personal(payload):
    """Strip personal-data keys from a snapshot payload, at any depth."""
    if isinstance(payload, dict):
        return {
            k: ("[redacted]" if k in PERSONAL_SNAPSHOT_KEYS else redact_personal(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact_personal(v) for v in payload]
    return payload


@app.get("/evidence/{snapshot_id}")
def evidence_item(snapshot_id: str, officer_id: str = Depends(current_officer)):
    rows = q(
        """SELECT id, entity_kind, entity_id, mmsi, kind, source, summary, payload,
                  captured_at
           FROM evidence_snapshots WHERE id=%s""",
        (snapshot_id,),
    )
    if not rows:
        raise HTTPException(404)
    row = dict(rows[0])
    row["payload"] = redact_personal(row.get("payload"))
    return row


@app.get("/network/{mmsi}")
def network(mmsi: int, depth: int = 2):
    """Ownership network for the UI (person names stay pseudonymous here; the Investigator tool decrypts)."""
    return q(
        "SELECT ownership_network(%s, %s) AS net",
        (f"vessel:{mmsi}", max(1, min(depth, 4))),
    )[0]["net"]


@app.post("/investigations/{inv_id}/fail")
def fail(inv_id: str, body: FailIn, request: Request):
    """Mark a running investigation failed. A finished case is never reopened."""
    actor = agent_actor(request, "orchestrator-agent")
    failed = q(
        """UPDATE investigations SET status='failed', report=%s, trace_id=%s,
                  updated_at=now()
            WHERE id=%s AND status='running' RETURNING id""",
        (jsonb({"error": body.error}), body.trace_id, inv_id),
    )
    if not failed:
        raise HTTPException(409, "investigation is not running")
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
    # Keep the newest entries only. This is an agent-only route, so a looping or
    # retrying orchestrator grew one JSONB value without bound, and every
    # /investigations page re-serialised the whole array (P9). The UI reads the tail,
    # so trimming the front is safe. One statement, because two workers may race here.
    q(
        """UPDATE jobs
              SET progress = (
                    SELECT coalesce(jsonb_agg(e ORDER BY i), '[]'::jsonb)
                      FROM (
                        SELECT e, i FROM jsonb_array_elements(progress || %s::jsonb)
                             WITH ORDINALITY AS t(e, i)
                             ORDER BY i DESC LIMIT %s
                      ) kept
                  ),
                  updated_at = now()
            WHERE id=%s""",
        (jsonb([entry]), PROGRESS_MAX_ENTRIES, rows[0]["job_id"]),
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
    # `report` is returned so the metric carries the report's priority; reviewing
    # twice is refused so the first officer's verdict stands (see review_alert).
    rows = q(
        """UPDATE investigations SET review_state=%s, reviewed_by=%s, reviewed_at=now()
            WHERE id=%s AND status='complete' AND review_state='draft'
        RETURNING id, review_state, reviewed_by, reviewed_at, trace_id, report""",
        (body.decision, officer_id, inv_id),
    )
    if not rows:
        already = q(
            "SELECT 1 FROM investigations WHERE id=%s AND status='complete' AND review_state<>'draft'",
            (inv_id,),
        )
        raise HTTPException(
            409 if already else 404,
            "already reviewed"
            if already
            else "no completed investigation with that id",
        )
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
    return {k: v for k, v in rows[0].items() if k not in ("trace_id", "report")}


@app.post("/sweep", status_code=202)
def sweep(hours: float = 12, officer_id: str = Depends(current_officer)):
    """Queue a watch sweep. The worker calls the Watch agent directly."""
    hours = max(1.0, min(float(hours), TRACK_MAX_HOURS))
    job_id, created = enqueue(
        "sweep",
        # Bucketed to the minute. `sweep_key(..., 0, ...)` is a new key every second,
        # so the one-active-job index never collided and a held button queued a
        # ten-minute Watch job per second (P9).
        sweep_key(datetime.now(UTC), MANUAL_SWEEP_BUCKET_MIN, hours),
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
    r = sse_redis()

    async def gen():
        last = "$"
        try:
            while True:
                try:
                    res = await r.xread(
                        {EVENTS_STREAM: last}, block=SSE_BLOCK_MS, count=100
                    )
                except redis.RedisError as e:
                    # A dropped read must not end the stream: the watch floor's progress
                    # panel is this stream, and a closed one leaves an investigation
                    # looking stuck. Ping (which also keeps the proxy from idling us out)
                    # and read again.
                    log.warning("events stream read failed, retrying: %s", e)
                    yield {"event": "ping", "data": "{}"}
                    continue
                if not res:
                    yield {"event": "ping", "data": "{}"}
                    continue
                for _, entries in res:
                    for eid, fields in entries:
                        last = eid
                        yield {"event": "job", "data": fields[b"data"].decode()}
                await asyncio.sleep(0)
        finally:
            # The generator is cancelled when the client goes away. Without this the
            # per-request client and its pool were never closed, and the watch floor
            # reopens both streams on every 401 and every rollout (P17).
            await r.aclose()

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


def require_operator(request: Request) -> str:
    """The verified operator role, or 403. Used where a score becomes a release gate.

    An eval score is not an opinion, it is the thing that decides whether a prompt or a
    model change may ship. The endpoint accepted any score from anyone the API let in,
    so a signed-in officer, or an agent role before ADR-0019, could write a passing
    score for a suite that had not run (gap audit P12).
    """
    caller = getattr(request.state, "caller", None)
    allowed = officer_roles()
    if not allowed:
        # No operator role configured means local development: identity is not enforced.
        return getattr(caller, "role", None) or "local"
    if caller is None or caller.role not in allowed:
        raise HTTPException(
            403, "recording an eval run needs the operator role's caller token"
        )
    return caller.role


@app.post("/evals", status_code=201)
def record_eval(body: EvalRunIn, request: Request):
    role = require_operator(request)
    # The gate decides whether a change may ship, so it is audited like a decision:
    # the signed-in officer when there is one, else the IAM role that presented the
    # token. `actor_kind` stays `system` because the table's CHECK allows three
    # values and this is tooling, not a person at the watch floor (P19).
    actor = getattr(request.state, "officer", "") or f"iam:{role}"
    rows = q(
        "INSERT INTO eval_runs (suite, scenario, scores, passed, thresholds, code_revision, details) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, ts",
        (
            body.suite,
            body.scenario,
            jsonb(body.scores),
            body.passed,
            jsonb(body.thresholds),
            body.code_revision,
            jsonb(body.details),
        ),
    )
    audit(
        actor,
        "system",
        "eval.recorded",
        "eval_run",
        rows[0]["id"],
        {
            "suite": body.suite,
            "passed": body.passed,
            "scores": body.scores,
            "role": role,
        },
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


def label(value) -> str:
    """A Prometheus label value with the three characters the format reserves escaped.

    Values here come from the database, and one alert whose severity or suite name
    contained a quote or a newline used to corrupt the entire exposition, taking every
    other metric down with it, including the ones the alarms read (P10).
    """
    return (
        str("" if value is None else value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


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
            f'argus_jobs_24h{{kind="{label(r["kind"])}",status="{label(r["status"])}"}} {r["n"]}'
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
            f'argus_alerts_open_by_severity{{severity="{label(r["severity"])}"}} {r["n"]}'
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
            f'argus_investigations_by_status{{status="{label(r["status"])}"}} {r["n"]}'
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
                    f'argus_eval_score{{suite="{label(r["suite"])}",metric="{label(k)}"}} {float(v)}'
                )
        lines.append(
            f'argus_eval_passed{{suite="{label(r["suite"])}"}} {1 if r["passed"] else 0}'
        )
    lines.append(
        f'argus_build_info{{code_revision="{label(os.getenv("GIT_SHA", "unknown"))}",ais_mode="{label(os.getenv("AIS_MODE", "replay"))}"}} 1'
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
    r = sse_redis()

    async def gen():
        last = "$"
        try:
            while True:
                try:
                    res = await r.xread({"ais:positions": last}, block=5000, count=200)
                except redis.RedisError as e:
                    log.warning("position stream read failed, retrying: %s", e)
                    yield {"event": "ping", "data": "{}"}
                    continue
                if not res:
                    yield {"event": "ping", "data": "{}"}
                    continue
                for _, entries in res:
                    for eid, fields in entries:
                        last = eid
                        yield {"event": "position", "data": fields[b"data"].decode()}
                await asyncio.sleep(0)
        finally:
            await r.aclose()  # see /events (P17)

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
def ground_truth(request: Request, officer_id: str = Depends(current_officer)):
    """Injected anomalies from the scenario, for the evaluation harness only.

    This is the answer key: an agent that could read it would score perfectly without
    detecting anything. The docstring said "not visible to agents" and nothing enforced
    it; ADR-0019's allowlists closed most of it, and this closes the rest by asking for
    the operator role explicitly rather than relying on a middleware fallback (P11).
    """
    require_operator(request)
    truth = scenario_meta("ground_truth")
    return [] if truth is None else truth
