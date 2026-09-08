"""Watch areas (live mode): the catalogue is well-formed, matches the scenario boxes it
mirrors, and WATCH_AREAS selection keeps the scenario area first."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, "services/ais-replay")
from areas import (  # noqa: E402
    load_catalogue,
    select_areas,
    subscription_boxes,
    valid_bbox,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "data/areas.yaml"


def scenario_area(name):
    s = yaml.safe_load((ROOT / f"data/scenarios/{name}.yaml").read_text())
    return {"name": s["name"], "bbox": s["bbox"]}


def test_catalogue_is_well_formed_and_mirrors_the_scenarios():
    areas = load_catalogue(str(CATALOGUE))
    names = [a["name"] for a in areas]
    assert len(names) == len(set(names)) >= 4
    for a in areas:
        assert valid_bbox(a["bbox"]) and a["label"]
        width = a["bbox"]["max_lon"] - a["bbox"]["min_lon"]
        height = a["bbox"]["max_lat"] - a["bbox"]["min_lat"]
        assert width <= 20 and height <= 12, f"{a['name']} is too big to sweep"
    by_name = {a["name"]: a for a in areas}
    assert by_name["east_med"]["bbox"] == scenario_area("east_med_baseline")["bbox"]
    assert by_name["aegean"]["bbox"] == scenario_area("aegean_shadow")["bbox"]


def test_selection_keeps_the_scenario_first_and_skips_its_own_box():
    cat = load_catalogue(str(CATALOGUE))
    sc = scenario_area("east_med_baseline")
    assert [a["name"] for a in select_areas(sc, "scenario", cat)] == [sc["name"]]
    everything = select_areas(sc, "all", cat)
    assert everything[0]["name"] == sc["name"]
    assert "east_med" not in [a["name"] for a in everything]  # same box as the scenario
    assert len(everything) == len(cat)  # scenario replaces its mirror, the rest follow
    some = select_areas(sc, " black_sea, malacca ,black_sea", cat)
    assert [a["name"] for a in some] == [sc["name"], "black_sea", "malacca"]
    with pytest.raises(ValueError, match="unknown area"):
        select_areas(sc, "atlantis", cat)


def test_subscription_boxes_are_lat_lon_pairs():
    sc = scenario_area("aegean_shadow")
    boxes = subscription_boxes(select_areas(sc, "scenario", []))
    assert boxes == [[[33.6, 23.0], [35.4, 26.6]]]


def test_bbox_validation():
    assert valid_bbox({"min_lon": 0, "min_lat": 0, "max_lon": 1, "max_lat": 1})
    assert not valid_bbox({"min_lon": 1, "min_lat": 0, "max_lon": 0, "max_lat": 1})
    assert not valid_bbox({"min_lon": 0, "min_lat": 0, "max_lon": 1})
    assert not valid_bbox({"min_lon": "x", "min_lat": 0, "max_lon": 1, "max_lat": 1})
