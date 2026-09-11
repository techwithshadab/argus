"""Live AIS ingest drops what it cannot store and cleans what it can (P3).

AIS encodes "not available" out of range: latitude 91, longitude 181, course 360,
speed 102.3. PostGIS rejects those coordinates and the resulting DataError was not
caught, so a single bad message from the feed crash-looped the ingest task and took
the position stream down with it. Reports at (0, 0) are the other half: the null
island default put unrelated vessels on one point, which the detectors read as MMSI
spoofs and rendezvous.
"""

import sys
from pathlib import Path

sys.path.insert(0, "services/ais-replay")
from aisvalidate import (  # noqa: E402
    clean_cog,
    clean_sog,
    position_record,
    valid_position,
)

ROOT = Path(__file__).resolve().parents[1]
TS = "2026-09-09T00:00:00+00:00"


def report(**kw):
    base = {"UserID": 232003453, "Latitude": 35.1, "Longitude": 24.6, "Sog": 12.0}
    return {**base, **kw}


def test_the_not_available_sentinels_are_rejected():
    assert not valid_position(91.0, 24.6)
    assert not valid_position(35.1, 181.0)
    assert not valid_position(-91.0, 0.5)
    assert not valid_position(35.1, -181.0)


def test_null_island_is_not_a_position():
    assert not valid_position(0.0, 0.0)
    assert not valid_position(0.00001, -0.00001)
    # A genuine fix near the line is kept.
    assert valid_position(0.0, 24.6)
    assert valid_position(5.2, 0.0)


def test_real_positions_and_the_boundaries_survive():
    for lat, lon in ((35.1, 24.6), (-33.9, 151.2), (90.0, 180.0), (-90.0, -180.0)):
        assert valid_position(lat, lon), (lat, lon)


def test_junk_types_are_rejected_rather_than_raising():
    for lat, lon in ((None, 24.6), ("x", 24.6), (35.1, None), (float("nan"), 1.0)):
        assert not valid_position(lat, lon)


def test_speed_and_course_sentinels_become_zero_without_dropping_the_fix():
    assert clean_sog(102.3) == 0.0  # "not available"
    assert clean_sog(-1) == 0.0
    assert clean_sog(None) == 0.0
    assert clean_sog(12.5) == 12.5
    assert clean_cog(360.0) == 0.0  # "not available"
    assert clean_cog(None) == 0.0
    assert clean_cog(87.5) == 87.5
    # The position is still stored, with the unusable fields zeroed.
    rec = position_record(report(Sog=102.3, Cog=360.0), TS)
    assert rec is not None and rec["sog"] == 0.0 and rec["cog"] == 0.0


def test_position_record_drops_the_message_the_database_would_reject():
    assert position_record(report(Latitude=91.0), TS) is None
    assert position_record(report(Longitude=181.0), TS) is None
    assert position_record(report(Latitude=0.0, Longitude=0.0), TS) is None
    assert position_record({"Latitude": 35.1, "Longitude": 24.6}, TS) is None  # no mmsi
    assert position_record(report(UserID=0), TS) is None


def test_position_record_shape_matches_the_insert_and_the_stream():
    rec = position_record(report(NavigationalStatus=0), TS)
    assert rec == {
        "mmsi": 232003453,
        "ts": TS,
        "lon": 24.6,
        "lat": 35.1,
        "sog": 12.0,
        "cog": 0.0,
        "nav_status": "0",
        "source": "aisstream",
    }


def test_ingest_validates_and_never_dies_on_one_bad_message():
    src = (ROOT / "services/ais-replay/replay.py").read_text()
    loop = src.split("async def live_aisstream")[1]
    assert "position_record(" in loop
    assert "if rec is None:" in loop
    # A row the validator let through can still be rejected by the database. The
    # writes moved into flush_batch (P15), so the guard lives there and a failed
    # batch is retried row by row rather than lost whole.
    writer = src.split("async def flush_batch(")[1].split("\nasync def ", 1)[0]
    assert writer.count("except psycopg.Error") == 2
    assert "retrying row by row" in writer
    assert "health.dropped_one()" in writer


def test_the_module_is_in_the_image():
    dockerfile = (ROOT / "services/ais-replay/Dockerfile").read_text()
    assert "aisvalidate.py" in dockerfile
