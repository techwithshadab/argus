"""Safety layer for agent outputs (phase 4). Pure functions, unit-tested without any SDK.

Three checks run on every node output before it is trusted:
- schema validity (pydantic, done by the caller),
- evidence traceability: every evidence entry a report cites must come from a tool the
  specialists actually used, and every report indicator must be backed by at least one entry,
- the allowed-actions policy: recommended actions are advisory verbs a watch officer can take;
  anything that reads like an agent acting on the world is refused.

Tool outputs are data, never instructions: `mcp-servers/common/safety.untrusted()` marks free
text from external sources at the tool boundary; this module only checks the report."""

from __future__ import annotations

import re

# Verbs a watch officer may be advised to take. Matched case-insensitively at the start of, or
# anywhere in, a recommended action.
ALLOWED_ACTION_PATTERNS = (
    r"\bmonitor\b",
    r"\bcontinue (to )?monitor",
    r"\bno (further )?action\b",
    r"\bquery\b.*\bflag state\b",
    r"\bcontact\b.*\b(flag state|owner|operator|agent|coastguard|port)\b",
    r"\bshare\b.*\b(partners?|agenc(y|ies)|coastguard|navy|liaison|intelligence|authorit(y|ies))\b",
    r"\bnotify\b",
    r"\brequest\b.*\b(port state control|inspection|boarding|imagery|collection|re-look|relook)\b",
    r"\brequest\b.*\b(information|update[sd]?|details|records|documentation|clarification)\b",
    r"\b(propose|recommend|open|continue|conduct)\b.*\binvestigation\b",
    r"\bfurther investigation\b",
    r"\brecommend\b.*\b(boarding|inspection)\b",
    r"\b(propose|task|collect)\b.*\b(imagery|sar|optical|sentinel|satellite|patrol|re-look|relook)\b",
    r"\bboard(ing)?\b",
    r"\binspect(ion)?\b",
    r"\bescalate\b",
    r"\bopen\b.*\b(case|investigation|file)\b",
    r"\bverify\b|\bconfirm\b|\bcheck\b|\bcross-?check\b",
    r"\bflag\b.*\b(for|to)\b",
    r"\bwatch(list)?\b",
)
# Actions an agent must never recommend as if it could do them, or that are outside the mandate.
FORBIDDEN_ACTION_PATTERNS = (
    r"\b(intercept|seize|detain|arrest|fire|strike|attack|sink|ram|disable)\b",
    r"\bi (will|have|am going to|'ll)\b",  # text is lower-cased before matching
    r"\b(hack|access|exfiltrate|jam|spoof)\b",
)


def check_actions(actions: list[str]) -> list[str]:
    """Return the actions that violate the policy (empty list = all allowed)."""
    bad = []
    for a in actions:
        s = (a or "").strip()
        if not s:
            bad.append(a)
            continue
        low = s.lower()
        if any(re.search(p, low) for p in FORBIDDEN_ACTION_PATTERNS):
            bad.append(a)
            continue
        if not any(re.search(p, low) for p in ALLOWED_ACTION_PATTERNS):
            bad.append(a)
    return bad


#: Said when a report cites a tool but no specialist cited anything at all. Soft, not
#: hard: this is the both-branches-degraded case, which ADR-0020 already caps and
#: caveats, and failing it would destroy a report that honestly says it found little.
UNTRACEABLE_PROBLEM = (
    "evidence cannot be traced: no specialist cited any tool, so nothing in this "
    "report rests on a tool result"
)


def check_evidence(
    report_evidence: list[dict], known_sources: set[str], indicators: list[str]
) -> list[str]:
    """Problems with a report's evidence: unknown sources, or no evidence at all for indicators.

    Sources are matched exactly. Prefix matching let a report cite `ais` and satisfy
    every AIS tool, or invent `ais.find_ais_gaps_and_the_thing_I_imagined` and match
    the real one; both sides are canonical `server.tool` by the time they arrive
    (`canonical_source`), so leniency bought nothing and cost the guarantee (A6).
    """
    problems = []
    known = {s.strip() for s in known_sources if s and s.strip()}
    for e in report_evidence:
        src = (e.get("source") or "").strip()
        if not src:
            problems.append("evidence entry without a source")
        elif not known:
            # An empty known set is the degraded case, not a licence to trust the
            # model: skipping the check here was skipping it exactly when a report
            # was most likely to be invented.
            if UNTRACEABLE_PROBLEM not in problems:
                problems.append(UNTRACEABLE_PROBLEM)
        elif src not in known:
            problems.append(f"evidence source not used by any specialist: {src}")
    if indicators and not report_evidence:
        problems.append("indicators present but no evidence cited")
    return problems


BALANCE_PROBLEM = (
    "no counter-indicators or information gaps stated: fill counter_indicators with what "
    "argues against the assessment, or information_gaps with what could not be established"
)
SOFT_PROBLEMS = (BALANCE_PROBLEM, UNTRACEABLE_PROBLEM)


def hard_problems(problems: list[str]) -> list[str]:
    """Policy problems that must block a report (everything but the soft ones)."""
    return [p for p in problems if p not in SOFT_PROBLEMS]


def validate_report(report: dict, known_sources: set[str]) -> list[str]:
    """All policy problems with a Vessel of Interest report. Empty means it passes."""
    problems = [
        f"action not allowed: {a}"
        for a in check_actions(report.get("recommended_actions") or [])
    ]
    problems += check_evidence(
        report.get("evidence") or [], known_sources, report.get("indicators") or []
    )
    if not (report.get("counter_indicators") or report.get("information_gaps")):
        # Balance is part of the mandate: a case with no stated counter-indicators and no
        # stated gaps reads as certainty the evidence rarely supports. Soft: the report node
        # accepts a report that still lacks them after the retry and says so in the caveats.
        problems.append(BALANCE_PROBLEM)
    return problems
