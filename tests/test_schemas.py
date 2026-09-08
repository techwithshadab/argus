import sys

sys.path.insert(0, "agents")
from shared.schemas import VesselOfInterestReport  # noqa: E402


def test_report_roundtrip():
    r = VesselOfInterestReport(
        mmsi=1,
        vessel_name="X",
        headline="h",
        priority="high",
        confidence="moderate",
        summary="s",
        timeline=["t"],
        identity_and_ownership="i",
        sanctions_and_compliance="s",
        indicators=["a"],
        recommended_actions=["r"],
        collection_plan="c",
        evidence=[{"source": "ais.find_ais_gaps", "summary": "gap"}],
    )
    assert VesselOfInterestReport.model_validate_json(r.model_dump_json()).mmsi == 1
