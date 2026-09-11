"""A collection is proposed where the vessel is, not where the model guessed (A2).

The Tasking agent has no AIS tool. It was handed the evidence gap as prose and asked
for an area of interest, so it produced coordinates from nothing: a live run proposed
a SAR collection over New York for a vessel off Singapore. An officer approving a
request cannot check a coordinate by eye, and a wrong AOI reads exactly like a right
one, so the position is resolved in code from the track the platform already holds
and the returned AOI is checked against it before the recommendation is offered.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "agents")

ROOT = Path(__file__).resolve().parents[1]

# Off Singapore, and the position the model actually proposed.
SINGAPORE = (103.8, 1.29)
NEW_YORK = (-74.0, 40.7)

TRACK = [
    {"ts": "2026-09-01T00:00:00+00:00", "lat": 1.20, "lon": 103.70},
    {"ts": "2026-09-01T02:00:00+00:00", "lat": 1.29, "lon": 103.80},
    {"ts": "2026-09-01T09:00:00+00:00", "lat": 1.55, "lon": 104.20},
]
ALERT = {
    "kind": "ais_gap",
    "started_at": "2026-09-01T02:30:00+00:00",
    "ended_at": "2026-09-01T08:30:00+00:00",
}


def test_distance_is_great_circle_nautical_miles():
    from shared.graph import nm_between

    assert nm_between(SINGAPORE, SINGAPORE) == 0.0
    # One degree of latitude is 60 nm by definition.
    assert nm_between((0.0, 0.0), (0.0, 1.0)) == pytest.approx(60.0, abs=0.2)
    # Singapore to New York is about 8300 nm great circle.
    assert nm_between(SINGAPORE, NEW_YORK) == pytest.approx(8300, rel=0.05)


def test_the_gap_position_is_the_last_report_before_the_window():
    from shared.graph import gap_position

    pos = gap_position(ALERT, TRACK)
    assert (pos["lon"], pos["lat"]) == (103.80, 1.29)
    assert pos["ts"] == "2026-09-01T02:00:00+00:00"


def test_the_newest_report_is_used_when_there_is_no_window():
    from shared.graph import gap_position

    pos = gap_position({}, TRACK)
    assert pos["ts"] == "2026-09-01T09:00:00+00:00"
    assert gap_position(None, TRACK)["ts"] == "2026-09-01T09:00:00+00:00"


def test_an_unordered_or_partial_track_still_resolves():
    from shared.graph import gap_position

    messy = [TRACK[2], {"ts": "x", "lat": None, "lon": 1.0}, TRACK[0], TRACK[1]]
    assert gap_position(ALERT, messy)["lon"] == 103.80


def test_no_track_gives_no_position_rather_than_a_guess():
    from shared.graph import gap_position

    assert gap_position(ALERT, []) is None
    assert gap_position(ALERT, None) is None
    assert gap_position(ALERT, [{"ts": "t"}]) is None


def test_the_new_york_aoi_for_a_singapore_vessel_is_rejected():
    from shared.graph import check_aoi, gap_position

    position = gap_position(ALERT, TRACK)
    rec = {"recommended": True, "aoi_center": list(NEW_YORK), "aoi_radius_nm": 20}
    problems = check_aoi(rec, position)
    assert problems and "from the vessel's last known position" in problems[0]


def test_an_aoi_at_the_gap_position_is_accepted():
    from shared.graph import check_aoi, gap_position

    position = gap_position(ALERT, TRACK)
    rec = {
        "recommended": True,
        "aoi_center": [103.85, 1.31],
        "aoi_radius_nm": 15,
        "sensor": "sar",
        "window_start": "2026-09-01T02:30:00+00:00",
        "window_end": "2026-09-01T08:30:00+00:00",
    }
    assert check_aoi(rec, position) == []


def test_a_recommendation_against_collection_needs_no_aoi():
    from shared.graph import check_aoi

    assert check_aoi({"recommended": False}, None) == []


def test_impossible_coordinates_radius_sensor_and_window_are_caught():
    from shared.graph import check_aoi

    assert check_aoi({"recommended": True, "aoi_center": [999, 0]}, None)
    assert check_aoi({"recommended": True, "aoi_center": "somewhere"}, None)
    assert check_aoi({"recommended": True}, None)
    assert check_aoi(
        {"recommended": True, "aoi_center": [103.8, 1.3], "aoi_radius_nm": 5000}, None
    )
    assert check_aoi(
        {"recommended": True, "aoi_center": [103.8, 1.3], "sensor": "telepathy"}, None
    )
    assert check_aoi(
        {
            "recommended": True,
            "aoi_center": [103.8, 1.3],
            "window_start": "2026-09-02T00:00:00+00:00",
            "window_end": "2026-09-01T00:00:00+00:00",
        },
        None,
    )


def test_the_orchestrator_states_the_position_and_drops_a_bad_proposal():
    src = (ROOT / "agents/orchestrator/app.py").read_text()
    node = src.split("def tasking_node(", 1)[1].split("\ndef ", 1)[0]
    assert "last_known_position(mmsi, alert)" in node
    assert "Last known position" in node
    assert "check_aoi(rec, position)" in node
    assert "return None" in node.split("check_aoi(rec, position)", 1)[1]


def tasking_validator():
    """`_tasking_problem` alone, compiled from source.

    The imagery server imports httpx and psycopg at module level, and unit tests run
    with neither; the validator is pure, so its source is executed on its own.
    """
    src = (ROOT / "mcp-servers/servers/imagery.py").read_text()
    start = src.index("#: What this deployment can actually task")
    end = src.index("@mcp.tool()", start)
    ns: dict = {}
    exec(compile(src[start:end], "imagery-validator", "exec"), ns)  # noqa: S102
    return ns["_tasking_problem"]


def test_the_tasking_tool_refuses_a_request_it_cannot_stand_behind():
    _tasking_problem = tasking_validator()

    ok = dict(
        sensor="sentinel-1-sar",
        center_lon=103.8,
        center_lat=1.29,
        radius_nm=20,
        window_start="2026-09-01T00:00:00+00:00",
        window_end="2026-09-02T00:00:00+00:00",
        priority="routine",
    )
    assert _tasking_problem(**ok) is None
    assert _tasking_problem(**{**ok, "sensor": "spy-balloon"})
    assert _tasking_problem(**{**ok, "priority": "whenever"})
    assert _tasking_problem(**{**ok, "center_lat": 91})
    assert _tasking_problem(**{**ok, "radius_nm": 0.1})
    assert _tasking_problem(**{**ok, "radius_nm": 500})
    assert _tasking_problem(**{**ok, "window_end": "2026-08-01T00:00:00+00:00"})
    assert _tasking_problem(**{**ok, "window_start": ""})


def test_the_aoi_box_narrows_with_latitude():
    """Without cos(lat) a northern box is far wider than the radius asked for (A8)."""
    src = (ROOT / "mcp-servers/servers/imagery.py").read_text()
    body = src.split("def create_tasking_request(", 1)[1]
    assert "math.cos(math.radians(center_lat))" in body
    assert "d_lon" in body and "d_lat" in body
