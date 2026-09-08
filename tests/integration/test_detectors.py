"""Deterministic detector evals (phase 5): every injected anomaly in a scenario must be found by
the corresponding AIS detector, with a bounded number of false positives. No model involved.

Needs a PostGIS database: set DATABASE_URL (CI starts a postgis service; locally the compose
database works with DATABASE_URL=postgresql://argus:argus@localhost:5432/argus). Skipped otherwise.
The test loads the scenario itself into a scratch schema so it never touches replayed data."""

from __future__ import annotations

import glob
import os
import sys
from datetime import datetime

import pytest

pytestmark = pytest.mark.integration

DB = os.getenv("DATABASE_URL")
if not DB:
    pytest.skip(
        "DATABASE_URL not set; integration test needs PostGIS", allow_module_level=True
    )

psycopg = pytest.importorskip("psycopg")
sys.path.insert(0, ".")
sys.path.insert(0, "mcp-servers")
from data.synthetic.generator import ScenarioGenerator  # noqa: E402

SCENARIOS = sorted(glob.glob("data/scenarios/*.yaml"))
# kind -> (detector, kwargs, result keys holding the list of hits)
KIND_DETECTOR = {
    "ais_gap": ("find_ais_gaps", {"min_gap_minutes": 30}, ("gaps", "still_dark")),
    "mmsi_spoof": ("detect_mmsi_conflicts", {}, ("conflicts",)),
    "loitering": ("detect_loitering", {"min_duration_minutes": 90}, ("loitering",)),
    "rendezvous": ("detect_rendezvous", {}, ("rendezvous",)),  # default threshold
}


def hits(out: dict, keys: tuple[str, ...]) -> list[dict]:
    return [r for k in keys for r in (out.get(k) or []) if isinstance(r, dict)]


def _iso(ts) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def load(gen: ScenarioGenerator) -> None:
    with psycopg.connect(DB) as c:
        for f in sorted(glob.glob("data/sql/*.sql")):
            if not os.path.basename(f).startswith("000"):
                c.execute(open(f).read())
        c.execute("DELETE FROM positions; DELETE FROM zones; DELETE FROM alerts")
        for v in gen.vessels():
            c.execute(
                "INSERT INTO vessels (mmsi, imo, name, flag, ship_type, length_m) VALUES (%(mmsi)s,%(imo)s,%(name)s,%(flag)s,%(ship_type)s,%(length_m)s) ON CONFLICT (mmsi) DO UPDATE SET name=EXCLUDED.name",
                v,
            )
        for z in gen.zones():
            ring = z["polygon"] + [z["polygon"][0]]
            wkt = "POLYGON((" + ",".join(f"{lon} {lat}" for lon, lat in ring) + "))"
            c.execute(
                "INSERT INTO zones (name, kind, geom) VALUES (%s, %s, ST_GeogFromText(%s))",
                (z["name"], z["kind"], wkt),
            )
        c.execute(
            "SELECT positions_ensure_partitions(%s::date, %s::date)",
            (gen.positions[0].ts.date(), gen.positions[-1].ts.date()),
        )
        with c.cursor().copy(
            "COPY positions (mmsi, ts, geom, sog, cog, nav_status, source) FROM STDIN"
        ) as cp:
            for p in gen.positions:
                cp.write_row(
                    (
                        p.mmsi,
                        p.ts,
                        f"SRID=4326;POINT({p.lon} {p.lat})",
                        p.sog,
                        p.cog,
                        p.nav_status,
                        "synthetic",
                    )
                )
        c.commit()


@pytest.fixture(
    scope="module", params=SCENARIOS, ids=[os.path.basename(s) for s in SCENARIOS]
)
def scenario(request):
    gen = ScenarioGenerator.from_file(request.param).run()
    load(gen)
    os.environ["SCENARIO_END"] = gen.positions[-1].ts.isoformat()
    from servers import ais  # noqa: PLC0415  (needs DATABASE_URL and the loaded data)

    return gen, ais, gen.positions[-1].ts.isoformat()


def _detected(rows: list[dict], truth: dict) -> bool:
    for r in rows:
        mmsis = {r.get("mmsi"), r.get("mmsi_a"), r.get("mmsi_b")}
        if truth["mmsi"] not in mmsis:
            continue
        start = (
            r.get("started_at")
            or r.get("start")
            or r.get("gap_start")
            or r.get("first_seen")
        )
        end = (
            r.get("ended_at") or r.get("end") or r.get("gap_end") or r.get("last_seen")
        )
        if not (start and end):
            return True  # detector reports the vessel without a window (e.g. spoof): count it
        if _iso(start) <= _iso(truth["ended_at"]) and _iso(truth["started_at"]) <= _iso(
            end
        ):
            return True
    return False


def test_every_injected_anomaly_is_detected(scenario):
    gen, ais, end = scenario
    truth = gen.ground_truth()
    missed = []
    for t in truth:
        fn, kw, keys = KIND_DETECTOR[t["kind"]]
        out = getattr(ais, fn)(hours=24, until=end, **kw)
        if not _detected(hits(out, keys), t):
            missed.append((t["kind"], t["mmsi"]))
    assert not missed, f"detectors missed {missed}"


def test_false_positives_are_bounded(scenario):
    gen, ais, end = scenario
    truth = gen.ground_truth()
    benign = {v["mmsi"] for v in gen.vessels()} - {t["mmsi"] for t in truth}
    flagged = set()
    for fn, kw, keys in KIND_DETECTOR.values():
        out = getattr(ais, fn)(hours=24, until=end, **kw)
        for r in hits(out, keys):
            flagged |= {r.get("mmsi"), r.get("mmsi_a"), r.get("mmsi_b")} & benign
    # A fishing pattern or an anchored ship may look like loitering to a raw detector; the Watch
    # agent's judgement removes those. More than two benign vessels flagged is a detector bug.
    assert len(flagged) <= 2, (
        f"benign vessels flagged by raw detectors: {sorted(flagged)}"
    )
