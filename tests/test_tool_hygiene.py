"""Tool results are bounded, clock-anchored, exactly matched and marked (A5-A10, A14).

Each of these was a way for a tool to mislead the model quietly: a candidate with no
coordinates for a prompt that asks for a zone check, a pass estimate measured from the
wrong clock, an evidence source that matched by prefix, an unbounded result on a path
with no truncation, external feed text arriving unmarked, and detectors defaulting to
the last day when the alert was older than that.
"""

import sys
from pathlib import Path

sys.path.insert(0, "agents")
sys.path.insert(0, "mcp-servers")

ROOT = Path(__file__).resolve().parents[1]
AIS = (ROOT / "mcp-servers/servers/ais.py").read_text()
GEO = (ROOT / "mcp-servers/servers/geo.py").read_text()
IMAGERY = (ROOT / "mcp-servers/servers/imagery.py").read_text()
REGISTRY = (ROOT / "mcp-servers/servers/registry.py").read_text()


# ---- A5: candidates carry a position ----
def test_every_candidate_kind_carries_coordinates():
    from shared.sweep import candidates_from

    det = {
        "gaps": {
            "window": ["2026-09-01T00:00:00+00:00", "2026-09-01T12:00:00+00:00"],
            "gaps": [
                {
                    "mmsi": 1,
                    "gap_start": "2026-09-01T02:00:00+00:00",
                    "gap_end": "2026-09-01T08:00:00+00:00",
                    "gap_minutes": 360,
                    "distance_nm": 40,
                    "last_lat": 35.1,
                    "last_lon": 24.6,
                }
            ],
        },
        "loitering": {"loitering": [{"mmsi": 2, "lat": 35.2, "lon": 24.7}]},
        "rendezvous": {
            "rendezvous": [{"mmsi_a": 3, "mmsi_b": 4, "lat": 35.3, "lon": 24.8}]
        },
        "incursions": {
            "incursions": [{"mmsi": 5, "zone": "Cable", "lat": 35.4, "lon": 24.9}]
        },
    }
    for c in candidates_from(det, []):
        assert c.get("lat") is not None, c["kind"]
        assert c.get("lon") is not None, c["kind"]


def test_the_model_sees_the_position_it_is_asked_to_check():
    from shared.sweep import render_candidates

    c = {
        "id": "x-c1",
        "kind": "loitering",
        "mmsi": 2,
        "started_at": "a",
        "ended_at": "b",
        "summary": "s",
        "lat": 35.23456789,
        "lon": 24.71234567,
    }
    view = render_candidates([c])[0]
    assert view["lat"] == 35.2346 and view["lon"] == 24.7123


def test_a_candidate_without_a_position_omits_the_keys_rather_than_sending_null():
    from shared.sweep import render_candidates

    view = render_candidates(
        [
            {
                "id": "x",
                "kind": "ais_gap",
                "mmsi": 1,
                "started_at": "a",
                "ended_at": "b",
                "summary": "s",
            }
        ]
    )[0]
    assert "lat" not in view and "lon" not in view


def test_the_incursion_detector_returns_a_position():
    assert "ST_Centroid(ST_Collect(p.geom::geometry))" in AIS
    body = AIS.split("def list_zone_incursions(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert '"lat"' in body and '"lon"' in body


# ---- A6: evidence sources match exactly ----
def test_a_prefix_no_longer_satisfies_the_evidence_check():
    from shared.policy import check_evidence

    known = {"ais.find_ais_gaps", "registry.lookup_vessel"}
    assert check_evidence([{"source": "ais.find_ais_gaps"}], known, []) == []
    assert check_evidence([{"source": "ais"}], known, [])
    assert check_evidence([{"source": "ais.find_ais_gaps_and_more"}], known, [])


def test_an_empty_source_set_is_the_degraded_case_not_a_free_pass():
    from shared.policy import UNTRACEABLE_PROBLEM, check_evidence

    problems = check_evidence([{"source": "ais.find_ais_gaps"}], set(), [])
    assert problems == [UNTRACEABLE_PROBLEM]


def test_the_untraceable_case_is_soft_so_a_thin_honest_report_still_ships():
    from shared.policy import SOFT_PROBLEMS, UNTRACEABLE_PROBLEM, hard_problems

    assert UNTRACEABLE_PROBLEM in SOFT_PROBLEMS
    assert hard_problems([UNTRACEABLE_PROBLEM]) == []


def test_the_untraceable_problem_is_reported_once_not_per_entry():
    from shared.policy import check_evidence

    rows = [{"source": "ais.a"}, {"source": "ais.b"}, {"source": "ais.c"}]
    assert len(check_evidence(rows, set(), [])) == 1


def test_the_eval_scores_the_report_against_real_sources():
    evals = (ROOT / "evals/node_evals.py").read_text()
    assert "validate_report(rep, sources)" in evals
    assert "validate_report(rep, set())" not in evals
    assert '"entity_kind": "investigation"' in evals


# ---- A7: the imagery server reads the scenario clock ----
def test_the_pass_estimate_uses_the_scenario_clock():
    assert "def scenario_now()" in IMAGERY
    assert 'os.getenv("AIS_MODE", "replay")' in IMAGERY
    body = IMAGERY.split("def estimate_next_pass(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert "scenario_now()" in body
    assert "datetime.now(UTC)" not in body


def test_both_clock_reading_servers_are_given_the_clock():
    compose = (ROOT / "docker-compose.yml").read_text()
    for service in ("MCP_SERVER: ais", "MCP_SERVER: imagery"):
        block = compose.split(service, 1)[1][:400]
        assert "SCENARIO_END" in block, service
    stack = (ROOT / "infra/cdk/stacks/agents_stack.py").read_text()
    tool_env = stack.split("tool_env = {", 1)[1].split("}", 1)[0]
    assert '"SCENARIO_END": scenario_end' in tool_env


# ---- A8/A10: external text is marked ----
def test_copernicus_scene_names_reach_the_model_marked():
    assert 'untrusted(p["Name"], "copernicus")' in IMAGERY
    assert 'untrusted(p["Id"], "copernicus")' in IMAGERY


def test_the_synthetic_fallback_is_not_marked_as_external():
    body = IMAGERY.split("def _synthetic_scenes(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert "untrusted(" not in body, "locally generated names are not feed text"


def test_fleet_siblings_are_marked():
    body = REGISTRY.split("def fleet_associations(", 1)[1]
    assert 'untrusted(s.get("name"), "ais static")' in body


# ---- A10: sanctions matching is by token ----
def test_a_single_common_word_no_longer_matches_the_whole_registry():
    from common.safety import name_matches

    assert not name_matches("Star", "Pilgrim Cargo SA")
    assert not name_matches("A", "Atlantic Dry Bulk Management")
    assert not name_matches("", "anything")
    assert not name_matches("Halcyon", None)


def test_the_scenario_hits_still_match():
    from common.safety import name_matches

    assert name_matches(
        "Halcyon Marine Management FZE", "Halcyon Marine Management FZE"
    )
    assert name_matches("Navand Tankers Co", "Navand Tankers Co")
    # A subset of tokens matches, as an officer would expect.
    assert name_matches("Halcyon Marine", "Halcyon Marine Management FZE")
    assert name_matches("meridian star", "MERIDIAN STAR SHIPPING LTD")


def test_punctuation_and_case_do_not_decide_a_sanctions_hit():
    from common.safety import name_matches

    assert name_matches(
        "Meridian Star Shipping Ltd (Marshall Islands)",
        "MERIDIAN STAR SHIPPING LTD (MARSHALL ISLANDS)",
    )


# ---- A9: results are bounded ----
def test_the_listing_tools_are_capped_and_say_when_they_were():
    assert "MAX_ROWS" in AIS
    for tool in ("def list_vessels(", "def find_vessels_near("):
        body = AIS.split(tool, 1)[1].split("\n@mcp.tool", 1)[0]
        assert "LIMIT %s" in body, tool
        assert '"truncated"' in body, tool


def test_zone_geometry_is_not_sent_to_the_model():
    body = GEO.split("def list_zones(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert "ST_AsGeoJSON" not in body
    assert "point_in_zones" in body, "the docstring must say how to test containment"


def test_the_ui_still_gets_its_geometry_from_the_api():
    api = (ROOT / "services/api/main.py").read_text()
    assert "ST_AsGeoJSON" in api.split('@app.get("/zones")', 1)[1][:400]


# ---- A14: the detectors look at the alert ----
def test_the_detector_window_covers_the_alert():
    from shared.graph import detector_window

    hours, until = detector_window(
        {
            "started_at": "2026-09-01T02:00:00+00:00",
            "ended_at": "2026-09-01T08:00:00+00:00",
        }
    )
    assert until == "2026-09-01T08:00:00+00:00"
    assert hours >= 24


def test_a_long_alert_widens_the_window():
    from shared.graph import detector_window

    hours, _ = detector_window(
        {
            "started_at": "2026-08-29T00:00:00+00:00",
            "ended_at": "2026-09-01T00:00:00+00:00",
        }
    )
    assert hours >= 72 + 12


def test_no_alert_means_the_old_default():
    from shared.graph import detector_window

    assert detector_window({}) == (24.0, None)
    assert detector_window(None) == (24.0, None)


def test_a_malformed_timestamp_does_not_raise():
    from shared.graph import detector_window

    hours, until = detector_window(
        {"started_at": "not a date", "ended_at": "2026-09-01T00:00:00+00:00"}
    )
    assert hours == 24.0 and until == "2026-09-01T00:00:00+00:00"


def test_the_window_is_stated_in_the_request_and_the_prompt():
    orch = (ROOT / "agents/orchestrator/app.py").read_text()
    assert "detector_window(alert)" in orch
    assert "Detector window:" in orch
    prompt = (ROOT / "agents/shared/prompts/investigator.md").read_text()
    assert "Detector window" in prompt


# ---- A13: the manifest attests what was called ----
def test_the_manifest_records_the_url_that_was_actually_invoked():
    orch = (ROOT / "agents/orchestrator/app.py").read_text()
    node = orch.split("def tasking_node(", 1)[1].split("\ndef ", 1)[0]
    assert "resolved = agent_url(" in node
    assert "agent_provenance(resolved)" in node
    assert 'rec.get("provenance")' in node
    assert "agent_provenance(settings.a2a_tasking_url)" not in node
