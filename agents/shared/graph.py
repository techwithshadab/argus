"""Pure pieces of the investigation graph (phase 4, ADR-0003): merging the two Investigator
branches, extracting the evidence gap for the Tasking agent, and the material the report node
is allowed to see. No network, no SDK; unit-tested."""

from __future__ import annotations

import json
import re

from .schemas import Evidence, InvestigationFindings

_OUT_OF_SCOPE = re.compile(r"^\s*(out of scope|n/?a|not (in )?scope)\b", re.I)
CONFIDENCE_RANK = {"low": 0, "moderate": 1, "high": 2}
GUARDRAIL_MARK = "blocked by the Argus guardrail"
RUNTIME_SESSION_RE = r"^rt-([0-9a-f-]{36})-.*$"


def runtime_session_id(investigation_id: str, role: str) -> str:
    """AgentCore runtime session id for one runtime invocation inside an investigation:
    `rt-<investigation id>-<role>`. Parallel branches must not share a runtime session (the
    service answers a second concurrent call with 409), so each call gets its own; the
    collector rewrites the `session.id` the runtime stamps on every span back to the bare
    investigation id (RUNTIME_SESSION_RE), so every trace store groups the whole investigation as one
    session. Session ids must be at least 33 characters."""
    base = f"rt-{investigation_id or 'adhoc'}-{role}"
    return base if len(base) >= 33 else base + "-" * (33 - len(base))


def guardrail_blocked(text: str) -> bool:
    """True when the model answer is the guardrail's canned block message (input or output)."""
    return GUARDRAIL_MARK in (text or "")


def memory_messages(report_text: str, investigation_id: str, mmsi: int) -> list[tuple]:
    """The conversation pair written to AgentCore Memory after an investigation. Long-term
    extraction works on user/assistant turns, so the assessment is stored as an answer to a
    question rather than as a lone assistant message."""
    return [
        (
            f"Investigation {investigation_id or 'adhoc'} of vessel {mmsi} has concluded. "
            "What was assessed?",
            "USER",
        ),
        (report_text, "ASSISTANT"),
    ]


def recall_text(records: list[str], events: list[str], limit: int = 6) -> str:
    """Prior context for the branches: extracted long-term records first, then the raw
    assessments of the most recent investigations (extraction lags by minutes and can be
    empty). Deduplicated, newest raw events last so the cap keeps the freshest."""
    seen: list[str] = []
    for t in list(records) + list(events):
        t = (t or "").strip()
        if t and t not in seen:
            seen.append(t)
    return "\n".join(seen[:limit])


def json_object(text: str) -> dict:
    """The first complete JSON object in an agent's answer, ignoring anything after it
    (models often follow the JSON with prose, which made json.loads fail on "Extra data")."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in agent response")


def unwrap_schema_shape(obj: dict, marker: str) -> dict:
    """Models shown a JSON schema sometimes echo its shape: values nested under
    "properties", or {"value": ...} per field. `marker` is a field a real answer has."""
    if isinstance(obj.get("properties"), dict) and marker not in obj:
        obj = obj["properties"]
    out = {}
    for k, v in obj.items():
        if (
            isinstance(v, dict)
            and set(v) <= {"value", "type", "description"}
            and "value" in v
        ):
            v = v["value"]
        out[k] = v
    return out


FINDINGS_TEXT_FIELDS = (
    "identity",
    "ownership",
    "sanctions_exposure",
    "behaviour_summary",
    "assessment",
)
_IDENTITY_FIELDS = {"identity", "ownership", "sanctions_exposure"}


ALERT_KINDS = ("ais_gap", "mmsi_spoof", "loitering", "zone_incursion", "rendezvous")
_KIND_ALIASES = {
    "mmsi_conflict": "mmsi_spoof",
    "mmsi_conflicts": "mmsi_spoof",
    "spoof": "mmsi_spoof",
    "spoofing": "mmsi_spoof",
    "gap": "ais_gap",
    "ais_gaps": "ais_gap",
    "loiter": "loitering",
    "incursion": "zone_incursion",
    "zone_incursions": "zone_incursion",
}


def normalise_kind(kind: str) -> str:
    """Alert kinds as the platform and the ground truth name them. Models tend to echo the
    detector tool's name (`mmsi_conflict` from detect_mmsi_conflicts) instead of the kind."""
    k = (kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIASES.get(k, k)


def prefix_evidence_sources(evidence: list, tool_servers: dict | None) -> list:
    """Cite tools as `server.tool`. The gateway names them `ais___find_ais_gaps`, Strands
    `ais_find_ais_gaps`, LangChain's adapter bare; the policy and the evals expect the
    dotted form. `tool_servers` maps bare tool names to their server."""
    from .tools import canonical_source

    out = []
    for e in evidence:
        if isinstance(e, dict):
            src = str(e.get("source") or "").strip()
            if src:
                e = {**e, "source": canonical_source(src, tool_servers)}
        out.append(e)
    return out


def findings_payload(
    obj: dict, mmsi: int | None, scope: str, tool_servers: dict | None = None
) -> dict:
    """Normalise an Investigator answer before validation: unwrap schema shapes, restore
    the vessel id, and fill the fields a scoped branch legitimately left out ("out of
    scope" for the other branch's fields, "not established" for its own) so a thin answer
    about a vessel with no registry record still validates instead of failing the case."""
    out = unwrap_schema_shape(obj, "assessment")
    if mmsi is not None:
        out.setdefault("mmsi", mmsi)
    for f in FINDINGS_TEXT_FIELDS:
        v = out.get(f)
        if isinstance(v, list):
            out[f] = " ".join(str(x) for x in v)
        elif not isinstance(v, str) or not v.strip():
            if f == "assessment":
                out[f] = "No assessment given."
            elif scope == "identity" and f == "behaviour_summary":
                out[f] = "out of scope"
            elif scope == "behaviour" and f in _IDENTITY_FIELDS:
                out[f] = "out of scope"
            else:
                out[f] = "not established"
    for f in ("risk_indicators", "counter_indicators", "information_gaps", "evidence"):
        v = out.get(f)
        if isinstance(v, str):
            out[f] = [x.strip(" -•") for x in v.splitlines() if x.strip(" -•")]
        elif not isinstance(v, list):
            out[f] = []
    out["evidence"] = prefix_evidence_sources(out["evidence"], tool_servers)
    if out.get("confidence") not in ("low", "moderate", "high"):
        out["confidence"] = "low"
    out.setdefault("scope", scope)
    return out


def tasking_payload(obj: dict, mmsi: int) -> dict:
    """Normalise a Tasking agent answer before validation (see unwrap_schema_shape); the
    agent often omits the vessel id it was given."""
    out = unwrap_schema_shape(obj, "recommended")
    out.setdefault("mmsi", mmsi)
    if "recommended" not in out and "tasking_id" in out:
        out["recommended"] = bool(out.get("tasking_id"))
    return out


def data_caveats(ais_mode: str, sanctions_source: str, model_caveats: str = "") -> str:
    """The provenance line every report carries, from what this deployment actually uses:
    live AIS or a replay scenario, OpenSanctions or the local list. Never a demo disclaimer."""
    if (ais_mode or "replay").lower() == "live":
        positions = (
            "Positions are live AIS via AISStream; static data comes from the feed."
        )
    else:
        positions = "Positions are a replayed scenario, not live traffic."
    if (sanctions_source or "local").lower() == "opensanctions":
        sanctions = "Sanctions screening used OpenSanctions and the local list."
    else:
        sanctions = "Sanctions screening used the local list only."
    registry = "The vessel registry is this deployment's reference table and may be incomplete."
    extra = (model_caveats or "").strip()
    parts = [positions, sanctions, registry]
    if extra and "synthetic" not in extra.lower() and "demo" not in extra.lower():
        parts.append(extra)
    return " ".join(parts)


PRIOR_CONTEXT_LIMIT = 1500


def prior_context_block(prior: str, limit: int = PRIOR_CONTEXT_LIMIT) -> str:
    """What an Investigator branch is told about earlier investigations of the same vessel
    (from AgentCore Memory). Capped, clearly labelled as possibly stale, and framed as
    context to verify rather than facts to repeat."""
    text = (prior or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return (
        "\nPrior context from earlier investigations of this vessel (may be stale; verify "
        "against current tool output, do not cite it as evidence):\n" + text + "\n"
    )


def report_payload(obj: dict, mmsi: int, vessel_name: str) -> dict:
    """Normalise the report writer's JSON before validation: unwrap schema shapes and restore
    the identifiers it was given, so a small omission does not cost a retry."""
    out = unwrap_schema_shape(obj, "headline")
    out.setdefault("mmsi", mmsi)
    if not out.get("vessel_name"):
        out["vessel_name"] = vessel_name or str(mmsi)
    for f in ("indicators", "recommended_actions", "timeline", "evidence"):
        out.setdefault(f, [])
    return out


def usage_delta(before: dict, after: dict) -> dict:
    """Token usage of one model call when the agent only exposes running totals."""
    return {
        k: int(v) - int(before.get(k, 0) or 0)
        for k, v in (after or {}).items()
        if isinstance(v, int | float)
    }


def _pick(primary: str, secondary: str) -> str:
    """The value from the branch that owns the field, unless it declared it out of scope."""
    return secondary if _OUT_OF_SCOPE.match(primary or "") and secondary else primary


def merge_findings(
    identity: InvestigationFindings, behaviour: InvestigationFindings
) -> InvestigationFindings:
    """Identity branch owns identity/ownership/sanctions; behaviour branch owns behaviour and
    associations. Indicators, counter-indicators, evidence and gaps are unioned in order,
    de-duplicated. Confidence is the lower of the two; the assessment is both, labelled."""
    seen: set[tuple] = set()
    evidence: list[Evidence] = []
    for e in identity.evidence + behaviour.evidence:
        k = (e.source, e.summary, e.reference)
        if k not in seen:
            seen.add(k)
            evidence.append(e)

    def union(a: list[str], b: list[str]) -> list[str]:
        out, s = [], set()
        for x in a + b:
            if x and not _OUT_OF_SCOPE.match(x) and x not in s:
                s.add(x)
                out.append(x)
        return out

    conf = min(
        (identity.confidence, behaviour.confidence), key=lambda c: CONFIDENCE_RANK[c]
    )
    return InvestigationFindings(
        mmsi=identity.mmsi or behaviour.mmsi,
        identity=_pick(identity.identity, behaviour.identity),
        ownership=_pick(identity.ownership, behaviour.ownership),
        sanctions_exposure=_pick(
            identity.sanctions_exposure, behaviour.sanctions_exposure
        ),
        behaviour_summary=_pick(
            behaviour.behaviour_summary, identity.behaviour_summary
        ),
        risk_indicators=union(identity.risk_indicators, behaviour.risk_indicators),
        counter_indicators=union(
            identity.counter_indicators, behaviour.counter_indicators
        ),
        assessment=f"Identity and ownership: {identity.assessment}\nBehaviour: {behaviour.assessment}",
        confidence=conf,
        evidence=evidence,
        information_gaps=union(identity.information_gaps, behaviour.information_gaps),
        scope="full",
        provenance={"identity": identity.provenance, "behaviour": behaviour.provenance},
    )


def evidence_gap(findings: InvestigationFindings, alert: dict | None) -> str:
    """What the Tasking agent needs: where and when evidence is missing. Prefer the triggering
    alert's window, fall back to the behaviour narrative."""
    alert = alert or {}
    if alert.get("started_at") or alert.get("ended_at"):
        d = alert.get("details") or {}
        return (
            f"{alert.get('kind', 'anomaly')} from {alert.get('started_at')} to {alert.get('ended_at')}; "
            f"{d.get('rationale') or alert.get('rationale') or ''}"
        ).strip()
    return findings.behaviour_summary


def known_sources(findings: InvestigationFindings, tasking: dict | None) -> set[str]:
    """Tool sources the report may cite: everything the specialists cited, plus the tasking
    tools when a tasking recommendation exists."""
    src = {e.source for e in findings.evidence if e.source}
    if tasking:
        src |= {
            "imagery.search_sentinel_scenes",
            "imagery.estimate_next_pass",
            "imagery.create_tasking_request",
            "imagery.list_tasking_requests",
        }
    return src


def report_material(
    findings: InvestigationFindings, tasking: dict | None, prior: str
) -> str:
    """The only input the report node sees: validated JSON, not a chat transcript."""
    parts = [
        "Investigation findings (JSON):",
        findings.model_dump_json(indent=1, exclude={"provenance"}),
        "",
        "Tasking recommendation (JSON):",
        json.dumps(
            {k: v for k, v in (tasking or {}).items() if k != "provenance"}, indent=1
        )
        if tasking
        else "none (tasking agent unavailable)",
    ]
    if prior:
        parts += ["", "Prior assessments of this vessel from memory:", prior]
    parts += ["", "Write the Vessel of Interest report."]
    return "\n".join(parts)


def text_hash(text: str) -> str:
    """Short content hash of the prompt that actually ran (ADR-0006 provenance)."""
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def choose_report_prompt(
    default: str, bundle: dict | None, version: str | None, managed_version: str
) -> tuple[str, str]:
    """Pure: the effective report prompt and its provenance label. A bundle prompt wins
    only when present and non-empty; the label then names the bundle version so the
    manifest cannot attest to the managed prompt while a different one ran (B6)."""
    text = (bundle or {}).get("report_system_prompt")
    if text and str(text).strip():
        return str(text), f"bundle:{version or 'unknown'}"
    return default, managed_version
