"""Phase 4 pure logic: model tiers, the safety policy, and the investigation graph's merge."""

import sys

import pytest

sys.path.insert(0, "agents")
from shared.config import (  # noqa: E402
    DEFAULT_ROLE_TIERS,
    next_tier,
    resolve_role_tier,
    resolve_tier_model,
)
from shared.graph import (  # noqa: E402
    evidence_gap,
    known_sources,
    merge_findings,
    report_material,
)
from shared.policy import check_actions, validate_report  # noqa: E402
from shared.schemas import Evidence, InvestigationFindings  # noqa: E402


def test_tiers_and_escalation():
    assert (
        resolve_role_tier("watch") == "fast" and resolve_role_tier("report") == "strong"
    )
    assert resolve_role_tier("watch", "standard") == "standard"
    with pytest.raises(ValueError):
        resolve_role_tier("watch", "huge")
    assert resolve_tier_model("bedrock", "fast") == "us.amazon.nova-lite-v1:0"
    assert resolve_tier_model("bedrock", "strong", "custom-id") == "custom-id"
    assert (
        next_tier("fast") == "standard"
        and next_tier("standard") == "strong"
        and next_tier("strong") is None
    )
    assert set(DEFAULT_ROLE_TIERS) == {"watch", "tasking", "investigator", "report"}


@pytest.mark.parametrize(
    "action,ok",
    [
        ("Query the flag state about the AIS outage", True),
        ("Share the case file with the regional partner", True),
        ("Continue to monitor for a second dark period", True),
        ("Request port state control inspection at next call", True),
        ("Propose a Sentinel-1 SAR re-look over the rendezvous position", True),
        ("No further action", True),
        ("Intercept and seize the vessel", False),
        ("I will task the satellite now", False),
        ("Buy more fuel", False),
        ("", False),
    ],
)
def test_allowed_actions(action, ok):
    assert (check_actions([action]) == []) is ok


def test_validate_report_traces_evidence():
    report = {
        "recommended_actions": ["Monitor"],
        "indicators": ["gap in corridor"],
        "evidence": [
            {"source": "ais.find_ais_gaps", "summary": "gap", "reference": "g1"}
        ],
        "information_gaps": ["no imagery of the gap"],
    }
    assert validate_report(report, {"ais.find_ais_gaps", "geo.point_in_zones"}) == []
    bad = dict(report, evidence=[{"source": "made_up_tool", "summary": "x"}])
    assert any(
        "not used by any specialist" in p
        for p in validate_report(bad, {"ais.find_ais_gaps"})
    )
    assert any(
        "no evidence" in p for p in validate_report(dict(report, evidence=[]), set())
    )


def _findings(scope, **over):
    base = dict(
        mmsi=511666006,
        identity="MERIDIAN STAR, IMO 9410006, PW"
        if scope == "identity"
        else "out of scope",
        ownership="Halcyon Marine Management FZE operates"
        if scope == "identity"
        else "out of scope",
        sanctions_exposure="operator listed" if scope == "identity" else "out of scope",
        behaviour_summary="out of scope"
        if scope == "identity"
        else "61 min gap inside corridor",
        risk_indicators=["listed operator"]
        if scope == "identity"
        else ["AIS gap in corridor", "listed operator"],
        counter_indicators=[] if scope == "identity" else ["registry consistent"],
        assessment=f"{scope} assessment",
        confidence="high" if scope == "identity" else "moderate",
        evidence=[Evidence(source="registry.lookup_vessel", summary="record")]
        if scope == "identity"
        else [
            Evidence(source="ais.find_ais_gaps", summary="gap", reference="g1"),
            Evidence(source="registry.lookup_vessel", summary="record"),
        ],
        information_gaps=["out of scope"] if scope == "identity" else ["no imagery"],
        scope=scope,
        provenance={"tier": "strong", "attempts": 1},
    )
    base.update(over)
    return InvestigationFindings(**base)


def test_merge_findings_owns_fields_and_unions_lists():
    m = merge_findings(_findings("identity"), _findings("behaviour"))
    assert m.identity.startswith("MERIDIAN STAR") and m.behaviour_summary.startswith(
        "61 min"
    )
    assert m.risk_indicators == ["listed operator", "AIS gap in corridor"]
    assert m.counter_indicators == ["registry consistent"]
    assert [e.source for e in m.evidence] == [
        "registry.lookup_vessel",
        "ais.find_ais_gaps",
    ]
    assert m.confidence == "moderate" and m.scope == "full"
    assert m.information_gaps == ["no imagery"]
    assert "Identity and ownership:" in m.assessment and "Behaviour:" in m.assessment


def test_evidence_gap_and_sources_and_material():
    f = merge_findings(_findings("identity"), _findings("behaviour"))
    alert = {
        "kind": "ais_gap",
        "started_at": "06:26Z",
        "ended_at": "07:27Z",
        "details": {"rationale": "dark in corridor"},
    }
    assert evidence_gap(f, alert).startswith(
        "ais_gap from 06:26Z to 07:27Z; dark in corridor"
    )
    assert evidence_gap(f, None) == f.behaviour_summary
    assert known_sources(f, None) == {"registry.lookup_vessel", "ais.find_ais_gaps"}
    assert "imagery.create_tasking_request" in known_sources(f, {"recommended": True})
    mat = report_material(
        f,
        {"recommended": False, "rationale": "short gap", "provenance": {"x": 1}},
        "prior note",
    )
    assert (
        "Investigation findings (JSON)" in mat
        and "prior note" in mat
        and '"x": 1' not in mat
    )
