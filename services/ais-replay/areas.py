"""Watch areas for live AIS mode.

The scenario file gives the area the demo is built around; `data/areas.yaml` is a catalogue
of named regions that can be watched alongside it. WATCH_AREAS picks from the catalogue:
"all", "scenario" (the scenario box only) or a comma-separated list of names. The result is
published as `area.areas` (scenario first) so the UI can offer them as a filter, and every
box goes into the one AISStream subscription. Pure: no database, no network."""

from __future__ import annotations

KEYS = ("min_lon", "min_lat", "max_lon", "max_lat")


def valid_bbox(bbox: dict) -> bool:
    try:
        b = {k: float(bbox[k]) for k in KEYS}
    except (KeyError, TypeError, ValueError):
        return False
    return (
        -180 <= b["min_lon"] < b["max_lon"] <= 180
        and -90 <= b["min_lat"] < b["max_lat"] <= 90
    )


def load_catalogue(path: str) -> list[dict]:
    """The named areas in `path`; malformed entries raise so a bad catalogue fails at start."""
    import yaml

    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    out = []
    for entry in doc.get("areas") or []:
        name = str(entry.get("name") or "").strip()
        if not name or not valid_bbox(entry.get("bbox") or {}):
            raise ValueError(f"areas.yaml: bad entry {entry!r}")
        out.append(
            {
                "name": name,
                "label": str(entry.get("label") or name),
                "bbox": {k: float(entry["bbox"][k]) for k in KEYS},
            }
        )
    if len({a["name"] for a in out}) != len(out):
        raise ValueError("areas.yaml: duplicate area names")
    return out


def same_box(a: dict, b: dict) -> bool:
    return all(abs(float(a[k]) - float(b[k])) < 1e-6 for k in KEYS)


def select_areas(scenario_area: dict, spec: str, catalogue: list[dict]) -> list[dict]:
    """The scenario area first, then the catalogue areas `spec` names. A catalogue area
    with the scenario's own box is skipped (the catalogue lists the scenario regions too);
    an unknown name raises so a typo is noticed at start rather than watched as nothing."""
    first = {
        "name": scenario_area["name"],
        "label": scenario_area.get("label") or scenario_area["name"],
        "bbox": scenario_area["bbox"],
    }
    spec = (spec or "all").strip().lower()
    if spec == "scenario":
        return [first]
    if spec == "all":
        wanted = [a["name"] for a in catalogue]
    else:
        wanted = [n.strip() for n in spec.split(",") if n.strip()]
    by_name = {a["name"]: a for a in catalogue}
    out = [first]
    for name in wanted:
        if name not in by_name:
            raise ValueError(f"WATCH_AREAS names unknown area {name!r}")
        a = by_name[name]
        if same_box(a["bbox"], first["bbox"]) or any(x["name"] == name for x in out):
            continue
        out.append(dict(a))
    return out


def subscription_boxes(areas: list[dict]) -> list[list[list[float]]]:
    """AISStream's BoundingBoxes shape: [[[min_lat, min_lon], [max_lat, max_lon]], ...]."""
    return [
        [
            [a["bbox"]["min_lat"], a["bbox"]["min_lon"]],
            [a["bbox"]["max_lat"], a["bbox"]["max_lon"]],
        ]
        for a in areas
    ]
