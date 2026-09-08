"""Orchestrator: the investigation graph, in code (phase 4, ADR-0003).

POST /invocations {"mmsi": 511666006, "trigger": "ais_gap", "investigation_id": "...", "alert": {...}}
POST /invocations {"action": "sweep", "hours": 12}

    identity branch  ─┐
                      ├─ join ─ tasking ─ report ─ persist
    behaviour branch ─┘

Every edge is code: the two Investigator branches run in parallel over A2A, the join is a pure
merge, the Tasking agent gets the extracted evidence gap, and the report node is a tool-less
model call over validated JSON only. Model judgement lives inside the nodes. The report is
checked against the safety policy (allowed actions, evidence traceability) and retried once
with the violations; the manifest records every model, prompt, tool version and attempt."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from opentelemetry import trace
from pydantic import ValidationError
from shared import a2a, provenance
from shared.config import next_tier, settings
from shared.discovery import agent_url
from shared.graph import (
    choose_report_prompt,
    data_caveats,
    evidence_gap,
    findings_payload,
    guardrail_blocked,
    json_object,
    known_sources,
    memory_messages,
    merge_findings,
    prior_context_block,
    recall_text,
    report_material,
    report_payload,
    runtime_session_id,
    tasking_payload,
    text_hash,
    usage_delta,
)
from shared.models import for_tier, model_unavailable, strands_model
from shared.platform_client import post
from shared.policy import hard_problems, validate_report
from shared.prompts_loader import load_prompt, prompt_version
from shared.schemas import (
    InvestigationFindings,
    TaskingRecommendation,
    VesselOfInterestReport,
)
from shared.telemetry import configure_strands, current_trace_id
from strands import Agent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent-orchestrator")
configure_strands("agent-orchestrator")

app = BedrockAgentCoreApp()

MCP_URLS = {
    "ais": settings.mcp_ais_url,
    "registry": settings.mcp_registry_url,
    "geo": settings.mcp_geo_url,
    "imagery": settings.mcp_imagery_url,
}
NODE_TIMEOUT_S = int(os.getenv("NODE_TIMEOUT_S", "480"))


# ---------------- memory (optional) ----------------
class VesselMemory:
    """Per-vessel long-term memory on AgentCore Memory. No-op when AGENTCORE_MEMORY_ID is unset."""

    def __init__(self):
        self.client = None
        if settings.memory_id:
            from bedrock_agentcore.memory import MemoryClient

            self.client = MemoryClient(region_name=settings.aws_region)

    def recall(self, mmsi: int) -> str:
        if not self.client:
            return ""
        records: list[str] = []
        try:
            hits = self.client.retrieve_memories(
                memory_id=settings.memory_id,
                namespace=f"/argus/vessel/{mmsi}",
                query="previous assessments, indicators, outcomes",
                top_k=5,
            )
            records = [h.get("content", {}).get("text", "") for h in hits]
        except Exception as e:  # noqa: BLE001
            log.warning("memory recall failed: %s", e)
        return recall_text(records, self._recent_assessments(mmsi))

    def _recent_assessments(self, mmsi: int, sessions: int = 3) -> list[str]:
        """Raw assessments of the latest investigations, straight from the stored events:
        long-term extraction lags by minutes, and an operator re-running a vessel expects the
        previous run to be known immediately."""
        try:
            import boto3

            dp = boto3.client("bedrock-agentcore", region_name=settings.aws_region)
            found = dp.list_sessions(
                memoryId=settings.memory_id, actorId=str(mmsi), maxResults=20
            ).get("sessionSummaries", [])
            found.sort(key=lambda x: str(x.get("createdAt")))
            out: list[str] = []
            for sess in found[-sessions:]:
                events = dp.list_events(
                    memoryId=settings.memory_id,
                    actorId=str(mmsi),
                    sessionId=sess["sessionId"],
                    includePayloads=True,
                    maxResults=5,
                ).get("events", [])
                for ev in events:
                    for part in ev.get("payload", []):
                        conv = part.get("conversational") or {}
                        if conv.get("role") == "ASSISTANT":
                            out.append(conv.get("content", {}).get("text", ""))
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("memory event read failed: %s", e)
            return []

    def remember(
        self, mmsi: int, report: VesselOfInterestReport, investigation_id: str = ""
    ) -> None:
        if not self.client:
            return
        try:
            text = (
                f"Assessment: {report.headline} Priority {report.priority}, confidence "
                f"{report.confidence}. Indicators: {'; '.join(report.indicators)}"
            )
            self.client.create_event(
                memory_id=settings.memory_id,
                actor_id=str(mmsi),
                session_id=f"inv-{current_trace_id() or 'na'}",
                messages=memory_messages(text, investigation_id, mmsi),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("memory write failed: %s", e)


memory = VesselMemory()


# ---------------- progress and provenance ----------------
def report_progress(
    investigation_id: str, step: str, status: str, detail: str = ""
) -> None:
    if not investigation_id:
        return
    try:
        post(
            f"/investigations/{investigation_id}/progress",
            {"step": step, "status": status, "detail": detail},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("progress report failed: %s", e)


def agent_provenance(url: str) -> dict:
    """Model and prompt the specialist at `url` is running (served on its /provenance route).
    A runtime or gateway invocation URL has no such route (the gateway answers the probe
    with a 403 "Missing Authentication Token" body), so those are not probed: their
    provenance comes back inside the A2A answer instead."""
    if "/invocations" in url:
        return {"transport": "agentcore"}
    try:
        import httpx

        r = httpx.get(url.rstrip("/") + "/provenance", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


class Run:
    """One investigation: collects node provenance and reports progress."""

    def __init__(self, investigation_id: str):
        self.investigation_id = investigation_id
        self.nodes: list[dict] = []
        self.prior_context_chars = 0

    def node(self, name: str, agent: str, attempts: int = 1, **info) -> None:
        self.nodes.append({"node": name, "agent": agent, "attempts": attempts, **info})

    def step(self, name: str, status: str, detail: str = "") -> None:
        report_progress(self.investigation_id, name, status, detail)


# ---------------- nodes ----------------
def investigator_branch(
    run: Run,
    scope: str,
    mmsi: int,
    trigger: str,
    alert: dict,
    prior: str = "",
) -> InvestigationFindings:
    run.step(f"investigator_{scope}", "started")
    msg = (
        f"Scope: {scope}\nVessel MMSI: {mmsi}\nTrigger: {trigger}\n"
        f"Investigation: {run.investigation_id or 'adhoc'}\n"
        f"Triggering alert: {json.dumps(alert)}\n"
        + prior_context_block(prior)
        + "Return the InvestigationFindings JSON for your scope only."
    )
    try:
        text = a2a.send(
            agent_url("investigator", settings.a2a_investigator_url),
            msg,
            timeout=NODE_TIMEOUT_S,
            session_id=runtime_session_id(run.investigation_id, scope),
        )
        data = a2a.json_object(text)
        if "error" in data and "identity" not in data:
            raise RuntimeError(f"investigator ({scope}) failed: {data['error']}")
        findings = InvestigationFindings.model_validate(
            findings_payload(data, mmsi, scope)
        )
    except Exception as e:  # noqa: BLE001
        # One branch failing must not lose the other's evidence: degrade to an empty
        # finding that names the gap. The caller fails the case only if both degrade.
        err = str(e)[:300]
        log.warning("investigator (%s) degraded: %s", scope, err)
        run.node(f"investigator_{scope}", "investigator", error=err, degraded=True)
        run.step(f"investigator_{scope}", "failed", err[:200])
        return InvestigationFindings.model_validate(
            findings_payload(
                {
                    "assessment": f"The {scope} branch did not complete.",
                    "information_gaps": [f"{scope} branch failed: {err}"],
                    "provenance": {"degraded": True},
                },
                mmsi,
                scope,
            )
        )
    run.node(
        f"investigator_{scope}",
        "investigator",
        attempts=int(findings.provenance.get("attempts", 1)),
        **{k: v for k, v in findings.provenance.items() if k != "attempts"},
    )  # includes provider, model_id, tier, prompt and usage from the specialist
    run.step(
        f"investigator_{scope}",
        "finished",
        f"{len(findings.risk_indicators)} indicators, confidence {findings.confidence}",
    )
    return findings


def tasking_node(
    run: Run, mmsi: int, findings: InvestigationFindings, alert: dict
) -> dict | None:
    run.step("tasking", "started")
    gap = evidence_gap(findings, alert)
    msg = (
        f"Vessel MMSI: {mmsi}\nBehaviour summary: {findings.behaviour_summary}\n"
        f"Evidence gap (position and time where evidence is missing): {gap}\n"
        "Decide whether collection would help and, if so, propose it. Return the TaskingRecommendation JSON."
    )
    try:
        if settings.tasking_harness_arn:
            text = harness_text(
                settings.tasking_harness_arn,
                msg,
                runtime_session_id(run.investigation_id, "tasking"),
            )
        else:
            text = a2a.send(
                agent_url("tasking", settings.a2a_tasking_url),
                msg,
                timeout=NODE_TIMEOUT_S,
                session_id=runtime_session_id(run.investigation_id, "tasking"),
            )
        rec = TaskingRecommendation.model_validate(
            tasking_payload(a2a.json_object(text), mmsi)
        ).model_dump()
    except Exception as e:  # noqa: BLE001
        # Tasking is advisory: a failure here must not lose the investigation.
        log.warning("tasking node failed: %s", e)
        run.node("tasking", "tasking", error=str(e)[:300])
        run.step("tasking", "failed", str(e)[:200])
        return None
    run.node("tasking", "tasking", **agent_provenance(settings.a2a_tasking_url))
    run.step(
        "tasking",
        "finished",
        "collection proposed"
        if rec.get("recommended")
        else "no collection recommended",
    )
    return rec


def harness_text(harness_arn: str, message: str, session_id: str) -> str:
    """One turn against an AgentCore Harness (the managed agent loop); returns the
    assistant's text. The Tasking pilot runs this way when TASKING_HARNESS_ARN is set."""
    import boto3

    client = boto3.client("bedrock-agentcore", region_name=settings.aws_region)
    resp = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": message}]}],
    )
    parts: list[str] = []
    for event in resp["stream"]:
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            parts.append(delta["text"])
        if "messageStop" in event and event["messageStop"].get("stopReason") not in (
            "end_turn",
            None,
        ):
            log.warning("harness stopped: %s", event["messageStop"].get("stopReason"))
    return "".join(parts)


def bundled_report_prompt(default: str) -> tuple[str, str]:
    """(prompt text, provenance label). The request's configuration bundle (AgentCore
    optimization: A/B tests, recommendations) may carry a `report_system_prompt`; it is
    honoured only when BUNDLE_OVERRIDE is on (ADR-0014), and the manifest then records the
    bundle version and the hash of the prompt that actually ran, not the managed one."""
    if not settings.bundle_override:
        return default, prompt_version("report")
    try:
        from bedrock_agentcore.runtime import BedrockAgentCoreContext

        config = BedrockAgentCoreContext.get_config_bundle() or {}
        ref = getattr(BedrockAgentCoreContext, "get_config_bundle_ref", lambda: None)()
        version = getattr(ref, "version_id", None) or getattr(ref, "version", None)
        return choose_report_prompt(default, config, version, prompt_version("report"))
    except Exception:  # noqa: BLE001
        return default, prompt_version("report")


def report_node(
    run: Run, findings: InvestigationFindings, tasking: dict | None, prior: str
) -> VesselOfInterestReport:
    """Tool-less model call over validated material, checked against the policy, one retry."""
    run.step("report", "started", "drafting the Vessel of Interest report")
    s, tier = for_tier("report")
    report_prompt, report_prompt_version = bundled_report_prompt(
        load_prompt("report", scenario_end=settings.scenario_end)
    )

    def build_agent(t: str) -> Agent:
        return Agent(
            name="Report writer",
            model=strands_model(role="report", tier=t),
            system_prompt=report_prompt,
            tools=[],
            trace_attributes={
                "agent.role": "report",
                "deployment.environment": settings.deploy_env,
                "session.id": run.investigation_id or "adhoc",
            },
        )

    agent = build_agent(tier)
    escalated = False
    sources = known_sources(findings, tasking)
    schema = json.dumps(VesselOfInterestReport.model_json_schema())
    material = (
        report_material(findings, tasking, prior)
        + "\n\nReturn only one JSON object, no prose, matching this schema:\n"
        + schema
    )
    problems: list[str] = []
    seen_usage: dict = {}
    blocked = False
    # Two model calls in the normal path; a third only when the guardrail blocked the retry
    # (the prompt-attack filter reads the whole prompt, and a correction list can trip it).
    for attempt in (1, 2, 3):
        if attempt == 1:
            prompt = material
        elif blocked:
            prompt = material + "\n\nCorrections:\n- " + "\n- ".join(problems)
        else:
            prompt = (
                material
                + "\n\nCorrections required for the previous draft (apply these and keep "
                + "everything else):\n- "
                + "\n- ".join(problems)
            )
        # A plain call rather than structured_output: the event loop then meters tokens, so
        # the manifest and the cost SLI include the report node.
        try:
            text = str(agent(prompt))
        except Exception as e:  # noqa: BLE001
            up = next_tier(tier)
            if escalated or up is None or not model_unavailable(e):
                raise
            # The tier's model is throttled or unavailable after the SDK's own retries:
            # one step up (a different model, its own quota) rather than a lost report.
            log.warning(
                "report tier %s unavailable (%s); escalating to %s",
                tier,
                str(e)[:120],
                up,
            )
            run.node(
                "report",
                "report",
                attempts=attempt,
                tier=tier,
                error=str(e)[:200],
                escalated_to=up,
            )
            escalated, tier = True, up
            s, _ = for_tier("report", tier)
            agent, seen_usage = build_agent(tier), {}
            text = str(agent(prompt))
        totals = dict(
            getattr(getattr(agent, "event_loop_metrics", None), "accumulated_usage", {})
            or {}
        )
        usage, seen_usage = usage_delta(seen_usage, totals), totals
        usage = {k: int(v) for k, v in usage.items() if isinstance(v, int | float)}
        if guardrail_blocked(text):
            run.node(
                "report",
                "report",
                attempts=attempt,
                guardrail_blocked=True,
                usage=usage,
            )
            log.warning("report attempt %d blocked by the guardrail", attempt)
            if blocked or attempt == 3:
                problems = ["guardrail blocked the report prompt twice"]
                break
            blocked = True
            continue
        try:
            report = VesselOfInterestReport.model_validate(
                report_payload(
                    json_object(text), findings.mmsi, findings.identity.split(",")[0]
                )
            )
        except (ValidationError, ValueError) as e:
            # The draft did not fit the schema: retry once with the errors, like a policy miss.
            errs = (
                e.errors()
                if isinstance(e, ValidationError)
                else [{"loc": (), "msg": str(e)}]
            )
            problems = [f"schema: {err['loc']}: {err['msg']}" for err in errs][:6]
            run.node(
                "report",
                "report",
                attempts=attempt,
                schema_problems=problems,
                usage=usage,
            )
            log.warning("report attempt %d failed validation: %s", attempt, problems)
            if attempt >= 2:
                break
            continue
        report.caveats = data_caveats(
            settings.ais_mode, settings.sanctions_source, report.caveats
        )
        problems = validate_report(report.model_dump(), sources)
        run.node(
            "report",
            "report",
            attempts=attempt,
            provider=s.model_provider,
            model_id=s.model_id,
            tier=tier,
            prompt=text_hash(report_prompt),
            prompt_version=report_prompt_version,
            policy_problems=problems,
            usage=usage,
        )
        if not problems or (attempt >= 2 and not hard_problems(problems)):
            if problems:
                # Only soft problems left after the retry: keep the report, say so in it.
                log.warning("report accepted with soft policy problems: %s", problems)
                report.caveats = (
                    report.caveats.rstrip()
                    + " The report states no counter-indicators or information gaps."
                ).strip()
            run.step(
                "report",
                "finished",
                f"priority {report.priority}, confidence {report.confidence}",
            )
            return report
        log.warning("report attempt %d violated policy: %s", attempt, problems)
        if attempt >= 2:
            break
    run.step("report", "failed", "; ".join(problems)[:200])
    raise RuntimeError("report rejected by policy after retry: " + "; ".join(problems))


def persist(
    investigation_id: str, report: VesselOfInterestReport, manifest: dict
) -> None:
    """Store the report through the platform API and in per-vessel memory. Done in code, not by a
    model, so a forgotten tool call can never lose a report."""
    # Aurora is the system of record; memory is derived working context (ADR-0015).
    # The database write comes first, so a failed completion never leaves a ghost
    # assessment that the next investigation would recall.
    if investigation_id:
        post(
            f"/investigations/{investigation_id}/complete",
            {
                "report": report.model_dump(),
                "trace_id": current_trace_id(),
                "manifest": manifest,
            },
        )
    memory.remember(report.mmsi, report, investigation_id)


# ---------------- graph ----------------
def run_investigation(payload: dict) -> dict:
    mmsi = int(payload["mmsi"])
    investigation_id = payload.get("investigation_id", "")
    trigger = payload.get("trigger", "manual")
    alert = payload.get("alert") or {}
    run = Run(investigation_id)
    # Session and user on the root span: one session per investigation in every trace store.
    span = trace.get_current_span()
    span.set_attribute("session.id", investigation_id or "adhoc")
    span.set_attribute("user.id", str(payload.get("requested_by") or trigger))
    span.set_attribute("argus.mmsi", mmsi)
    try:
        prior = memory.recall(mmsi)
        run.prior_context_chars = len(prior or "")
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(
                investigator_branch, run, "identity", mmsi, trigger, alert, prior
            )
            fb = ex.submit(
                investigator_branch, run, "behaviour", mmsi, trigger, alert, prior
            )
            fa_r, fb_r = fa.result(), fb.result()
            if fa_r.provenance.get("degraded") and fb_r.provenance.get("degraded"):
                raise RuntimeError("both investigator branches failed")
            findings = merge_findings(fa_r, fb_r)
        run.step(
            "join", "finished", f"{len(findings.evidence)} evidence entries merged"
        )
        tasking = tasking_node(run, mmsi, findings, alert)
        report = report_node(run, findings, tasking, prior)
        manifest = provenance.manifest(
            run.nodes,
            provenance.mcp_versions(MCP_URLS),
            {
                "trigger": trigger,
                "alert_id": alert.get("id"),
                "prior_context_chars": run.prior_context_chars,
            },
        )
        persist(investigation_id, report, manifest)
        return {
            "report": report.model_dump(),
            "trace_id": current_trace_id(),
            "manifest": manifest,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("investigation failed")
        if investigation_id:
            post(
                f"/investigations/{investigation_id}/fail",
                {"error": str(e), "trace_id": current_trace_id()},
            )
        raise


def run_sweep(hours: float) -> dict:
    """Kept for callers that still route sweeps through the orchestrator; the worker calls the
    Watch agent directly."""
    text = a2a.send(
        agent_url("watch", settings.a2a_watch_url),
        f"Sweep the last {hours:g} hours of AIS and raise alerts. Return your JSON summary.",
        timeout=NODE_TIMEOUT_S,
    )
    return {"result": text, "trace_id": current_trace_id()}


@app.entrypoint
def invoke(payload: dict, context: Any = None) -> dict:
    if payload.get("action") == "sweep":
        return run_sweep(float(payload.get("hours", 12)))
    return run_investigation(payload)


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "8080")))
