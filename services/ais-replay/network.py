"""Build the ownership network (entities and edges) from registry rows. Pure: no database.

The loader in replay.py persists the result with entity_upsert / edge_upsert (003_ownership_network.sql).
Persons are personal data: their real name is returned separately so the loader can encrypt it, and
the entity's display name is a pseudonymous label."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

COMPANY_MARKERS = (
    "ltd",
    "limited",
    "inc",
    "corp",
    "corporation",
    "llc",
    "plc",
    "gmbh",
    "sa",
    "s.a",
    "as",
    "ag",
    "bv",
    "nv",
    "pte",
    "fze",
    "fzco",
    "co",
    "company",
    "holdings",
    "holding",
    "group",
    "management",
    "shipping",
    "carriers",
    "tankers",
    "cargo",
    "marine",
    "maritime",
    "lines",
    "logistics",
    "trading",
    "enterprises",
    "services",
    "partners",
    "capital",
    "fund",
    "trust",
    "unknown",
    "nominee",
)


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower())).strip()


def is_person(name: str) -> bool:
    """A beneficial owner without any company marker is treated as a natural person."""
    words = set(norm(name).split())
    return bool(words) and not (words & set(COMPANY_MARKERS))


def pseudonym(name: str) -> str:
    return "person-" + hashlib.sha256(norm(name).encode()).hexdigest()[:8]


@dataclass
class Entity:
    kind: str
    key: str
    name: str
    attrs: dict = field(default_factory=dict)
    real_name: str | None = None  # persons only; encrypted by the loader


@dataclass
class Edge:
    src: str  # entity key
    dst: str
    rel: str
    source: str = "registry"
    since: str | None = None
    until: str | None = None
    confidence: float = 1.0
    attrs: dict = field(default_factory=dict)


def _month(s: str | None) -> str | None:
    """'2025-11' -> '2025-11-01T00:00:00Z'; None passes through."""
    if not s:
        return None
    s = str(s)
    return s + "-01T00:00:00Z" if len(s) == 7 else s


def build_graph(registry_rows: list[dict]) -> tuple[dict[str, Entity], list[Edge]]:
    ents: dict[str, Entity] = {}
    edges: list[Edge] = []

    def ent(
        kind: str,
        ident: str,
        name: str,
        attrs: dict | None = None,
        person: bool = False,
    ) -> str:
        # vessels are keyed by identifier (mmsi or imo:<n>) verbatim; names are normalised
        key = f"{kind}:{ident if kind == 'vessel' else norm(ident)}"
        if key not in ents:
            ents[key] = Entity(
                kind,
                key,
                pseudonym(name) if person else name,
                dict(attrs or {}),
                name if person else None,
            )
        elif attrs:
            ents[key].attrs.update(attrs)
        return key

    def party(name: str | None) -> str | None:
        if not name:
            return None
        if is_person(name):
            return ent("person", name, name, person=True)
        return ent("company", name, name)

    for r in registry_rows:
        mmsi = r["mmsi"]
        v = ent(
            "vessel",
            str(mmsi),
            r.get("name") or str(mmsi),
            {"mmsi": mmsi, "imo": r.get("imo"), "flag": r.get("flag")},
        )
        for rel, who in (
            ("owns", r.get("registered_owner")),
            ("operates", r.get("operator")),
            ("beneficially_owns", r.get("beneficial_owner")),
        ):
            p = party(who)
            if p:
                edges.append(Edge(p, v, rel))
        for s in r.get("sanctions") or []:
            listing = ent(
                "sanction_listing", s.get("list", "unknown"), s.get("list", "unknown")
            )
            target = party(s.get("entry")) or v
            edges.append(
                Edge(
                    target,
                    listing,
                    "listed_on",
                    since=_month(s.get("since")),
                    attrs={"entry": s.get("entry")},
                )
            )
        if r.get("flag"):
            edges.append(Edge(v, ent("flag", r["flag"], r["flag"]), "flagged"))
        for h in r.get("flag_history") or []:
            if h.get("until") and h.get("flag") and h["flag"] != r.get("flag"):
                edges.append(
                    Edge(
                        v,
                        ent("flag", h["flag"], h["flag"]),
                        "reflagged_from",
                        until=_month(h.get("until")),
                    )
                )
        owner = party(r.get("registered_owner"))
        for f in r.get("fleet") or []:
            sib = ent(
                "vessel",
                f"imo:{f.get('imo')}" if f.get("imo") else f.get("name", "?"),
                f.get("name") or str(f.get("imo")),
                {"imo": f.get("imo"), "declared_fleet_of": mmsi},
            )
            if owner:
                edges.append(
                    Edge(
                        owner, sib, "owns", confidence=0.7, attrs={"declared_by": mmsi}
                    )
                )
            edges.append(Edge(sib, v, "fleet_of", confidence=0.7))
    return ents, edges


def rendezvous_edges(pairs: list[dict]) -> list[Edge]:
    """pairs: rows with mmsi_a, mmsi_b, started_at, ended_at, minutes (from the AIS proximity query)."""
    return [
        Edge(
            f"vessel:{p['mmsi_a']}",
            f"vessel:{p['mmsi_b']}",
            "rendezvoused_with",
            source="ais",
            since=str(p["started_at"]),
            until=str(p["ended_at"]),
            confidence=0.8,
            attrs={"minutes": p["minutes"]},
        )
        for p in pairs
    ]
