"""A half-finished investigation cannot claim a whole one's confidence (A3).

One Investigator branch failing does not lose the other's evidence: the branch degrades
to an empty finding that names the gap, and the case continues. The report model then
sees fluent material and set its own confidence from it, so a run with a failed
behaviour branch returned high confidence and high priority on identity evidence alone,
with nothing in the report saying half of it was missing. The cap, the gap entry and
the caveat are applied in code, after validation, so a retry cannot argue them away.
"""

import sys

import pytest

sys.path.insert(0, "agents")


def findings(identity_degraded=False, behaviour_degraded=False, confidence="high"):
    from shared.schemas import InvestigationFindings

    return InvestigationFindings.model_validate(
        {
            "mmsi": 232003453,
            "identity": "TAMARA, Panama flagged",
            "ownership": "one shell company",
            "sanctions_exposure": "no direct match",
            "behaviour_summary": "six hour gap",
            "risk_indicators": ["AIS gap near a declared zone"],
            "counter_indicators": [],
            "assessment": "worth a look",
            "confidence": confidence,
            "evidence": [],
            "information_gaps": [],
            "scope": "full",
            "provenance": {
                "identity": {"degraded": True}
                if identity_degraded
                else {"tier": "pro"},
                "behaviour": (
                    {"degraded": True} if behaviour_degraded else {"tier": "pro"}
                ),
            },
        }
    )


def report(confidence="high", priority="high"):
    return {
        "mmsi": 232003453,
        "vessel_name": "TAMARA",
        "headline": "Vessel went dark near a restricted zone",
        "priority": priority,
        "confidence": confidence,
        "summary": "The vessel stopped reporting for six hours.",
        "timeline": ["2026-09-01T02:00Z last report"],
        "identity_and_ownership": "Panama flagged, one shell company.",
        "sanctions_and_compliance": "No direct match.",
        "indicators": ["AIS gap near a declared zone"],
        "counter_indicators": [],
        "recommended_actions": ["monitor"],
        "collection_plan": "SAR re-look over the gap position.",
        "evidence": [],
        "information_gaps": [],
        "caveats": "Positions are live AIS via AISStream.",
    }


def test_a_complete_investigation_is_untouched():
    from shared.graph import cap_for_degraded

    r = report()
    assert cap_for_degraded(r, findings()) == r


def test_a_failed_branch_caps_confidence_and_priority():
    from shared.graph import cap_for_degraded

    out = cap_for_degraded(report(), findings(behaviour_degraded=True))
    assert out["confidence"] == "moderate"
    assert out["priority"] == "medium"


def test_the_cap_never_raises_a_modest_report():
    from shared.graph import cap_for_degraded

    out = cap_for_degraded(
        report(confidence="low", priority="low"),
        findings(behaviour_degraded=True, confidence="low"),
    )
    assert out["confidence"] == "low"
    assert out["priority"] == "low"


def test_the_cap_never_exceeds_the_findings_own_confidence():
    from shared.graph import cap_for_degraded

    out = cap_for_degraded(
        report(), findings(behaviour_degraded=True, confidence="low")
    )
    assert out["confidence"] == "low"


def test_the_missing_branch_is_named_in_the_gaps_and_the_caveats():
    from shared.graph import cap_for_degraded

    out = cap_for_degraded(report(), findings(identity_degraded=True))
    assert any(
        "identity" in g and "did not complete" in g for g in out["information_gaps"]
    )
    assert "identity" in out["caveats"] and "capped" in out["caveats"]
    # The data provenance caveat already there is kept.
    assert "AISStream" in out["caveats"]


def test_both_branches_are_named_when_both_degraded():
    from shared.graph import cap_for_degraded

    out = cap_for_degraded(
        report(), findings(identity_degraded=True, behaviour_degraded=True)
    )
    assert "identity and behaviour" in out["caveats"]


def test_applying_the_cap_twice_changes_nothing_further():
    from shared.graph import cap_for_degraded

    f = findings(behaviour_degraded=True)
    once = cap_for_degraded(report(), f)
    assert cap_for_degraded(once, f) == once


def test_the_capped_report_still_matches_the_schema():
    from shared.graph import cap_for_degraded
    from shared.schemas import VesselOfInterestReport

    out = cap_for_degraded(report(), findings(behaviour_degraded=True))
    assert VesselOfInterestReport.model_validate(out).confidence == "moderate"


def test_the_input_report_is_not_mutated():
    from shared.graph import cap_for_degraded

    r = report()
    cap_for_degraded(r, findings(behaviour_degraded=True))
    assert r["confidence"] == "high" and r["information_gaps"] == []


def test_degraded_scopes_reads_the_orchestrators_provenance_shape():
    from shared.graph import degraded_scopes

    assert degraded_scopes(findings()) == []
    assert degraded_scopes(findings(identity_degraded=True)) == ["identity"]
    assert degraded_scopes(
        findings(identity_degraded=True, behaviour_degraded=True)
    ) == ["identity", "behaviour"]


def test_the_report_node_caps_before_the_policy_check():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "agents/orchestrator/app.py"
    ).read_text()
    node = src.split("def report_node(", 1)[1]
    cap = node.index("cap_for_degraded(")
    policy = node.index("validate_report(report.model_dump(), sources)")
    assert cap < policy, "the cap must be applied before the report is judged"


@pytest.mark.parametrize("scope", ["identity", "behaviour"])
def test_either_branch_alone_triggers_the_cap(scope):
    from shared.graph import cap_for_degraded

    f = findings(**{f"{scope}_degraded": True})
    assert cap_for_degraded(report(), f)["confidence"] == "moderate"
