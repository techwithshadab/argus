"""Ownership-network construction (pure) and the archiver's pure helpers."""

import sys
from datetime import date

import pytest

sys.path.insert(0, "services/ais-replay")
sys.path.insert(0, "services/archiver")
from network import (  # noqa: E402
    build_graph,
    is_person,
    norm,
    pseudonym,
    rendezvous_edges,
)

REGISTRY = [
    {
        "mmsi": 511666006,
        "imo": 9410006,
        "name": "MERIDIAN STAR",
        "flag": "PW",
        "flag_history": [
            {"flag": "PA", "until": "2024-03"},
            {"flag": "GA", "until": "2025-08"},
            {"flag": "PW", "until": None},
        ],
        "registered_owner": "Meridian Star Shipping Ltd (Marshall Islands)",
        "operator": "Halcyon Marine Management FZE",
        "beneficial_owner": "Unknown (nominee directors)",
        "sanctions": [
            {
                "list": "FICTIONAL-OFAC-SDN",
                "entry": "Halcyon Marine Management FZE",
                "since": "2025-11",
            }
        ],
        "fleet": [{"name": "MERIDIAN DAWN", "imo": 9410016}],
    },
    {
        "mmsi": 422888009,
        "name": "NAVAND 3",
        "flag": "IR",
        "flag_history": [{"flag": "IR", "until": None}],
        "registered_owner": "Navand Tankers Co",
        "operator": "Navand Tankers Co",
        "beneficial_owner": "Farid Navand",
        "sanctions": [],
        "fleet": [],
    },
]


@pytest.mark.parametrize(
    "name,person",
    [
        ("Farid Navand", True),
        ("Halcyon Marine Management FZE", False),
        ("Pilgrim Holdings (Cyprus)", False),
        ("Unknown (nominee directors)", False),
        ("Atlantic Dry Bulk Management", False),
        ("Maria Rossi", True),
    ],
)
def test_person_heuristic(name, person):
    assert is_person(name) is person


def test_pseudonym_is_stable_and_not_the_name():
    assert pseudonym("Farid Navand") == pseudonym("farid  navand")
    assert "navand" not in pseudonym("Farid Navand")
    assert norm("Halcyon Marine, Management (FZE)") == "halcyon marine management fze"


def test_build_graph_shapes():
    ents, edges = build_graph(REGISTRY)
    kinds = {
        k: sum(1 for e in ents.values() if e.kind == k)
        for k in ("vessel", "company", "person", "sanction_listing", "flag")
    }
    assert kinds["vessel"] == 3  # two registry vessels + one declared fleet sibling
    assert kinds["person"] == 1 and kinds["sanction_listing"] == 1
    person = next(e for e in ents.values() if e.kind == "person")
    assert person.real_name == "Farid Navand" and person.name.startswith("person-")
    rels = {(e.src, e.rel, e.dst) for e in edges}
    assert (
        "company:halcyon marine management fze",
        "operates",
        "vessel:511666006",
    ) in rels
    assert (
        "company:halcyon marine management fze",
        "listed_on",
        "sanction_listing:fictional ofac sdn",
    ) in rels
    assert ("vessel:511666006", "reflagged_from", "flag:pa") in rels
    assert ("vessel:511666006", "flagged", "flag:pw") in rels
    assert ("vessel:imo:9410016", "fleet_of", "vessel:511666006") in rels
    assert (person.key, "beneficially_owns", "vessel:422888009") in rels
    listed = next(e for e in edges if e.rel == "listed_on")
    assert listed.since == "2025-11-01T00:00:00Z"


def test_rendezvous_edges():
    e = rendezvous_edges(
        [
            {
                "mmsi_a": 1,
                "mmsi_b": 2,
                "started_at": "t0",
                "ended_at": "t1",
                "minutes": 40,
            }
        ]
    )[0]
    assert (e.src, e.dst, e.rel, e.source, e.attrs["minutes"]) == (
        "vessel:1",
        "vessel:2",
        "rendezvoused_with",
        "ais",
        40,
    )


def test_archive_key():
    from archive import archive_key

    assert (
        archive_key("positions", date(2026, 9, 1), "positions_p20260901")
        == "positions/day=2026-09-01/positions_p20260901.parquet"
    )


def test_datakey_copies_identical():
    a = open("mcp-servers/common/datakey.py").read()
    assert (
        a
        == open("services/api/datakey.py").read()
        == open("services/ais-replay/datakey.py").read()
    )
