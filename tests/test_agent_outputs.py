"""Agent output handling that the AWS demo exercised: JSON followed by prose, and report
fields the model returned in the wrong shape."""

import sys

import pytest

sys.path.insert(0, "agents")
from shared.graph import json_object  # noqa: E402
from shared.schemas import VesselOfInterestReport  # noqa: E402


def test_json_object_ignores_trailing_prose():
    text = 'Here it is:\n{"recommended": true, "reason": "gap"}\n\nLet me know if you need more.'
    assert json_object(text) == {"recommended": True, "reason": "gap"}


def test_json_object_skips_a_brace_in_prose():
    text = 'Use {braces} carefully. {"a": 1} {"b": 2}'
    assert json_object(text) == {"a": 1}


def test_json_object_without_object_raises():
    with pytest.raises(ValueError):
        json_object("no json here")


def _report(**over):
    base = {
        "mmsi": 511666006,
        "vessel_name": "MERIDIAN STAR",
        "headline": "h",
        "priority": "high",
        "confidence": "moderate",
        "summary": "s",
        "timeline": ["t"],
        "identity_and_ownership": "i",
        "sanctions_and_compliance": "c",
        "indicators": ["x"],
        "recommended_actions": ["Monitor"],
        "collection_plan": "none",
        "evidence": [],
    }
    base.update(over)
    return VesselOfInterestReport.model_validate(base)


def test_report_joins_list_given_for_prose_field():
    r = _report(collection_plan=["No imagery.", "Reassess tomorrow."])
    assert r.collection_plan == "No imagery. Reassess tomorrow."


def test_report_splits_prose_given_for_list_field():
    r = _report(indicators="- AIS gap of 61 minutes\n- Rendezvous with NAVAND 3")
    assert r.indicators == ["AIS gap of 61 minutes", "Rendezvous with NAVAND 3"]


def test_tasking_payload_unwraps_schema_shape_and_fills_mmsi():
    from shared.graph import tasking_payload

    raw = {
        "properties": {
            "recommended": True,
            "rationale": "gap",
            "sensor": {"value": "SAR"},
        }
    }
    out = tasking_payload(raw, 538009102)
    assert out == {
        "recommended": True,
        "rationale": "gap",
        "sensor": "SAR",
        "mmsi": 538009102,
    }


def test_tasking_payload_leaves_a_plain_answer_alone():
    from shared.graph import tasking_payload

    raw = {"mmsi": 1, "recommended": False, "rationale": "enough evidence"}
    assert tasking_payload(raw, 2) == raw


def test_findings_payload_fills_a_thin_identity_answer():
    from shared.graph import findings_payload
    from shared.schemas import InvestigationFindings

    raw = {
        "identity": "ENERGEAN STAR, MMSI 538009102, no registry record",
        "evidence": [],
    }
    f = InvestigationFindings.model_validate(
        findings_payload(raw, 538009102, "identity")
    )
    assert f.mmsi == 538009102 and f.scope == "identity"
    assert f.behaviour_summary == "out of scope"
    assert f.ownership == "not established" and f.confidence == "low"
    assert f.risk_indicators == []


def test_findings_payload_unwraps_schema_shape():
    from shared.graph import findings_payload

    raw = {"properties": {"assessment": {"value": "benign"}, "confidence": "moderate"}}
    out = findings_payload(raw, 7, "behaviour")
    assert out["assessment"] == "benign" and out["confidence"] == "moderate"
    assert out["identity"] == "out of scope" and out["mmsi"] == 7


def test_data_caveats_name_the_real_sources_and_drop_demo_text():
    from shared.graph import data_caveats

    live = data_caveats("live", "opensanctions", "All data in this demo is synthetic.")
    assert "live AIS via AISStream" in live and "OpenSanctions" in live
    assert "demo" not in live.lower() and "synthetic" not in live.lower()
    replay = data_caveats("replay", "local", "Track has a 20 minute gap.")
    assert "replayed scenario" in replay and "local list only" in replay
    assert replay.endswith("Track has a 20 minute gap.")
