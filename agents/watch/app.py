"""Watch Agent: Strands agent exposed over A2A. Sweeps a window of AIS and raises alerts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message
from shared.cards import CARDS, VERSION
from shared.config import settings
from shared.mcp_tools import wait_for_mcp
from shared.models import for_tier, strands_model
from shared.platform_client import post
from shared.prompts_loader import load_prompt, prompt_hash, prompt_version
from shared.sweep import (
    alert_evidence,
    candidates_from,
    expired_candidates,
    non_dismissible,
    rank_candidates,
    render_candidates,
    sweep_failure,
)
from shared.telemetry import configure_strands
from shared.tools import detector_client, health_urls, strands_tool_clients, tool_name
from starlette.responses import JSONResponse
from starlette.routing import Route
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-watch")
configure_strands("agent-watch")


# Candidates of the current sweeps, by id (see shared/sweep.py). Entries older than an
# hour are dropped so a long-running process does not grow without bound.
_CANDIDATES: dict[str, dict] = {}
_CANDIDATE_TS: dict[str, float] = {}
_DISPOSITIONS: dict[str, dict] = {}


def _detect(hours: float, until: str | None) -> dict:
    """Run the five detectors and the open-alert list through a short-lived MCP client."""
    args = {"hours": hours, **({"until": until} if until else {})}
    calls = {
        "gaps": (tool_name("ais", "find_ais_gaps"), args),
        "conflicts": (tool_name("ais", "detect_mmsi_conflicts"), args),
        "loitering": (tool_name("ais", "detect_loitering"), args),
        "rendezvous": (tool_name("ais", "detect_rendezvous"), args),
        "incursions": (tool_name("ais", "list_zone_incursions"), args),
        "open_alerts": (tool_name("ais", "list_open_alerts"), {}),
    }
    out: dict = {}
    failed: list[str] = []
    with detector_client() as c:
        for key, (name, a) in calls.items():
            res = c.call_tool_sync(f"sweep-{key}-{time.time_ns()}", name, a)
            text = "".join(
                part.get("text", "")
                for part in res.get("content", [])
                if "text" in part
            )
            # A policy deny, a SQL error or a gateway refusal arrives as an error
            # result. Nothing read that, so a broken detector produced `{}` and the
            # sweep reported a quiet sea (A4).
            if res.get("status") == "error" or res.get("isError"):
                log.warning("detector %s failed: %s", name, text[:200])
                failed.append(key)
                out[key] = {}
                continue
            try:
                out[key] = json.loads(text) if text else {}
            except json.JSONDecodeError:
                log.warning("detector %s returned no JSON: %s", name, text[:120])
                failed.append(key)
                out[key] = {}
    out["_failed"] = failed
    return out


@tool
def raise_alert(
    candidate_id: str,
    severity: str,
    score: float,
    rationale: str,
    extra_evidence: list[dict] | None = None,
) -> dict:
    """Raise an alert for a candidate from list_candidates. The vessel, kind, time window
    and detector evidence come from the candidate; you supply the judgement.

    Args:
        candidate_id: The candidate's id from list_candidates.
        severity: low, medium or high.
        score: 0 to 1 anomaly score.
        rationale: Why this matters, in one or two sentences.
        extra_evidence: Further {source, summary, reference} entries from the context tools you called.
    """
    c = _CANDIDATES.get(candidate_id)
    if not c:
        return {
            "error": f"unknown candidate {candidate_id}; call list_candidates first"
        }
    payload = {
        "mmsi": c["mmsi"],
        "kind": c["kind"],
        "severity": severity,
        "score": score,
        "rationale": rationale,
        "started_at": c["started_at"],
        "ended_at": c["ended_at"],
        # Canonicalised in pure code: through the gateway the model names tools
        # `geo___point_in_zones`, which no officer or scorer recognises (A1).
        "evidence": alert_evidence(c, extra_evidence),
        "created_by": "watch-agent",
    }
    try:
        created = post("/alerts", payload)
    except Exception as e:  # noqa: BLE001
        log.warning("raise_alert %s failed: %s", candidate_id, e)
        return {"error": f"alert not stored: {e}"}
    _DISPOSITIONS[candidate_id] = {"disposition": "raised", "severity": severity}
    log.info("alert raised for %s: %s %s", candidate_id, c["kind"], c["mmsi"])
    return created


@tool
def dismiss_candidate(candidate_id: str, reason: str) -> dict:
    """Record that a candidate is explainable and needs no alert (a fishing pattern, an
    anchorage, a short gap far from any zone).

    Args:
        candidate_id: The candidate's id from list_candidates.
        reason: Why it is benign, in one sentence.
    """
    if candidate_id not in _CANDIDATES:
        return {"error": f"unknown candidate {candidate_id}"}
    why = non_dismissible(_CANDIDATES[candidate_id])
    if why:
        return {"error": f"cannot dismiss {candidate_id}: {why}"}
    _DISPOSITIONS[candidate_id] = {"disposition": "dismissed", "reason": reason}
    return {"ok": True, "candidate_id": candidate_id}


def build_agent() -> Agent:
    return Agent(
        name="Watch Agent",
        description="Sweeps AIS traffic for a time window and raises evidenced anomaly alerts (AIS gaps, MMSI spoofing, loitering, rendezvous, zone incursions).",
        model=strands_model(role="watch"),
        # A sweep reads many tool results; on context overflow Strands truncates the oldest
        # tool results instead of failing the whole sweep.
        conversation_manager=SlidingWindowConversationManager(
            window_size=40, should_truncate_results=True
        ),
        system_prompt=load_prompt("watch", scenario_end=settings.scenario_end),
        tools=[
            *strands_tool_clients(["ais", "geo"]),
            raise_alert,
            dismiss_candidate,
        ],
        trace_attributes={
            "agent.role": "watch",
            "deployment.environment": settings.deploy_env,
        },
    )


class WatchExecutor(AgentExecutor):
    """A sweep is code around the model: the detectors decide what the candidates are, the
    model judges one candidate at a time in a fresh conversation, and the executor makes
    sure every candidate ends up raised or dismissed."""

    def __init__(self):
        self.agent = build_agent()
        self._lock = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        text = context.get_user_input()
        hours, until = parse_sweep_request(text)
        log.info("sweep request: %.1f h until %s", hours, until or "now")
        async with self._lock:  # one sweep at a time per process: the agent holds state
            summary = await asyncio.to_thread(self.sweep, hours, until)
        await event_queue.enqueue_event(
            new_agent_text_message(
                json.dumps(summary), context.context_id, context.task_id
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")

    def sweep(self, hours: float, until: str | None) -> dict:
        det = _detect(hours, until)
        failed = det.pop("_failed", [])
        # Every detector failing is a broken sweep, not a calm sea. Raising here fails
        # the job so it is retried and the alarm fires, instead of reporting zero
        # candidates exactly as a quiet watch does (A4).
        broken = sweep_failure(failed)
        if broken:
            raise RuntimeError(broken)
        open_alerts = (det.get("open_alerts") or {}).get("alerts") or []
        cands, deferred = rank_candidates(
            candidates_from(det, open_alerts), settings.sweep_max_candidates
        )
        now = time.time()
        # The runtime is long-lived and each sweep mints fresh ids, so nothing is ever
        # overwritten; prune before inserting this sweep's own candidates (A11).
        for cid in expired_candidates(_CANDIDATE_TS, now):
            _CANDIDATE_TS.pop(cid, None)
            _CANDIDATES.pop(cid, None)
            _DISPOSITIONS.pop(cid, None)
        for c in cands:
            _CANDIDATES[c["id"]] = c
            _CANDIDATE_TS[c["id"]] = now
        raised = dismissed = unreviewed = 0
        notes: list[str] = []
        if failed:
            notes.append(
                f"{len(failed)} detector(s) failed and found nothing: {', '.join(failed)}"
            )
        if deferred:
            notes.append(
                f"{len(deferred)} lower-ranked candidates deferred to the next sweep"
            )
        for c in cands:
            view = render_candidates([c])[0]
            self.agent.messages = []  # fresh conversation per candidate
            prompt = (
                "Review this candidate. Gather the context you need with the tools, then "
                f"call raise_alert or dismiss_candidate for candidate id {c['id']}.\n"
                + json.dumps(view)
            )
            try:
                self.agent(prompt)
                if c["id"] not in _DISPOSITIONS:
                    self.agent(
                        f"You have not disposed of candidate {c['id']} yet. Call "
                        "raise_alert or dismiss_candidate for it now."
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("candidate %s review failed: %s", c["id"], e)
            d = _DISPOSITIONS.get(c["id"])
            if not d:
                # The model never disposed of it. The detector evidence still stands, so
                # the officer sees it as a low alert that says exactly that; a candidate
                # must never vanish because a model turn ran out.
                unreviewed += 1
                res = raise_alert(
                    c["id"],
                    "low",
                    0.3,
                    "Detector candidate the model did not finish reviewing; the evidence "
                    "is the detector output only.",
                )
                notes.append(
                    f"{c['id']} {c['kind']} {c['mmsi']}: not reviewed, raised low"
                    + (
                        " (store failed)"
                        if isinstance(res, dict) and res.get("error")
                        else ""
                    )
                )
            elif d["disposition"] == "raised":
                raised += 1
            else:
                dismissed += 1
                notes.append(
                    f"{c['id']} {c['kind']} {c['mmsi']}: {d.get('reason', '')}"
                )
        return {
            "alerts_raised": raised,
            "dismissed": dismissed,
            "unreviewed": unreviewed,
            "candidates": len(cands),
            "deferred": len(deferred),
            "already_open": len(open_alerts),
            "vessels_reviewed": len({c["mmsi"] for c in cands}),
            "failed_detectors": failed,
            "notes": "; ".join(notes)[:2000],
        }


def parse_sweep_request(text: str) -> tuple[float, str | None]:
    """`hours` and optional `until` from the worker's sweep message."""
    m = re.search(r"last\s+(\d+(?:\.\d+)?)\s*h", text, re.I)
    # ISO-8601 characters only: `(\S+)` swallowed a trailing full stop, and
    # `fromisoformat` then raised inside all five detectors (A15).
    u = re.search(r"until\s+([0-9T:+\-]+(?:\.\d+)?Z?)", text, re.I)
    return (float(m.group(1)) if m else 12.0), (u.group(1) if u else None)


wait_for_mcp(health_urls())
card = AgentCard(
    name=CARDS["watch"]["name"],
    description=CARDS["watch"]["description"],
    url=settings.a2a_public_url or f"http://0.0.0.0:{settings.a2a_port}/",
    version=VERSION,
    capabilities=AgentCapabilities(streaming=False),
    default_input_modes=["text"],
    default_output_modes=["text"],
    skills=[AgentSkill(**sk) for sk in CARDS["watch"]["skills"]],
)
handler = DefaultRequestHandler(
    agent_executor=WatchExecutor(), task_store=InMemoryTaskStore()
)
app = A2AStarletteApplication(agent_card=card, http_handler=handler).build()


async def ping(_):
    return JSONResponse({"status": "Healthy"})


async def provenance_route(_):
    s, tier = for_tier("watch")
    return JSONResponse(
        {
            "provider": s.model_provider,
            "model_id": s.model_id,
            "tier": tier,
            "prompt": prompt_hash("watch"),
            "prompt_version": prompt_version("watch"),
        }
    )


app.router.routes.insert(0, Route("/ping", ping))
app.router.routes.insert(0, Route("/provenance", provenance_route))

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.a2a_port, log_level="info")
