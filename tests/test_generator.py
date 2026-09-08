from datetime import datetime

from data.synthetic.generator import ScenarioGenerator

SCENARIO = "data/scenarios/east_med_baseline.yaml"


def test_scenario_produces_positions_and_truth():
    g = ScenarioGenerator.from_file(SCENARIO).run()
    assert len(g.positions) > 3000
    kinds = {t["kind"] for t in g.ground_truth()}
    assert kinds == {"ais_gap", "loitering", "mmsi_spoof", "rendezvous"}


def test_dark_gap_has_no_broadcasts():
    g = ScenarioGenerator.from_file(SCENARIO).run()
    gap = next(t for t in g.ground_truth() if t["kind"] == "ais_gap")
    s, e = (
        datetime.fromisoformat(gap["started_at"]),
        datetime.fromisoformat(gap["ended_at"]),
    )
    inside = [p for p in g.positions if p.mmsi == gap["mmsi"] and s < p.ts < e]
    assert inside == []


def test_spoofed_mmsi_broadcasts_from_two_places():
    g = ScenarioGenerator.from_file(SCENARIO).run()
    spoof = next(t for t in g.ground_truth() if t["kind"] == "mmsi_spoof")
    pts = [p for p in g.positions if p.mmsi == spoof["mmsi"]]
    lats = [p.lat for p in pts]
    assert max(lats) - min(lats) > 0.5  # two tracks far apart, same MMSI
