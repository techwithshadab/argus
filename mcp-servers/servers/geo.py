"""Geo MCP server: zones, ports, distances, reverse geocoding.

Zones come from PostGIS. Ports use a small built-in gazetteer for the scenario area and
fall back to the OpenStreetMap Overpass API (no key) when GEO_USE_OSM=true."""

from __future__ import annotations

import json
import math
import os

import httpx
from common.db import q
from common.safety import untrusted
from common.telemetry import traced_tool
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "geo",
    instructions="Geospatial context: protected zones, nearest ports, distances and reverse geocoding.",
    stateless_http=True,
    json_response=True,
)

PORTS = [
    {"name": "Limassol", "country": "CY", "lon": 33.04, "lat": 34.66},
    {"name": "Larnaca", "country": "CY", "lon": 33.64, "lat": 34.92},
    {
        "name": "Vasilikos (energy terminal)",
        "country": "CY",
        "lon": 33.32,
        "lat": 34.72,
    },
    {"name": "Port Said", "country": "EG", "lon": 32.30, "lat": 31.26},
    {"name": "Beirut", "country": "LB", "lon": 35.50, "lat": 33.90},
    {"name": "Haifa", "country": "IL", "lon": 35.00, "lat": 32.82},
    {"name": "Mersin", "country": "TR", "lon": 34.63, "lat": 36.80},
]


def _nm(lon1, lat1, lon2, lat2) -> float:
    r = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@mcp.tool()
@traced_tool
def list_zones() -> dict:
    """All declared zones (protected cables, exclusion areas, port approaches, anchorages) with their properties."""
    rows = q(
        "SELECT id, name, kind, properties, ST_AsGeoJSON(geom::geometry) geojson FROM zones ORDER BY id"
    )
    return {"zones": rows}


@mcp.tool()
@traced_tool
def point_in_zones(lon: float, lat: float, buffer_nm: float = 0) -> dict:
    """Which zones contain (or are within `buffer_nm` of) a point."""
    rows = q(
        """SELECT name, kind, properties, ST_Distance(geom, ST_MakePoint(%s,%s)::geography)/1852 distance_nm
           FROM zones WHERE ST_DWithin(geom, ST_MakePoint(%s,%s)::geography, %s*1852) ORDER BY distance_nm""",
        (lon, lat, lon, lat, buffer_nm),
    )
    return {
        "lon": lon,
        "lat": lat,
        "zones": [
            {**r, "distance_nm": round(float(r["distance_nm"]), 2)} for r in rows
        ],
    }


@mcp.tool()
@traced_tool
def nearest_ports(lon: float, lat: float, limit: int = 3) -> dict:
    """Nearest ports to a point with distance in nautical miles."""
    ports = [
        {
            **p,
            "distance_nm": round(_nm(lon, lat, p["lon"], p["lat"]), 1),
            "source": "gazetteer",
        }
        for p in PORTS
    ]
    if os.getenv("GEO_USE_OSM", "false").lower() == "true":
        try:
            query = f'[out:json][timeout:10];node["harbour"="yes"](around:150000,{lat},{lon});out 20;'
            r = httpx.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                timeout=20,
                headers={"User-Agent": "argus/0.2"},
            )
            for el in r.json().get("elements", []):
                ports.append(
                    {
                        "name": untrusted(
                            el.get("tags", {}).get("name", "unnamed harbour"),
                            "osm overpass",
                        ),
                        "lon": el["lon"],
                        "lat": el["lat"],
                        "distance_nm": round(_nm(lon, lat, el["lon"], el["lat"]), 1),
                        "source": "osm-overpass",
                    }
                )
        except Exception as e:  # noqa: BLE001
            ports.append(
                {"name": "osm lookup failed", "error": str(e), "distance_nm": 1e9}
            )
    ports.sort(key=lambda p: p["distance_nm"])
    return {"ports": ports[:limit]}


@mcp.tool()
@traced_tool
def distance_nm(lon1: float, lat1: float, lon2: float, lat2: float) -> dict:
    """Great-circle distance between two points in nautical miles."""
    return {"distance_nm": round(_nm(lon1, lat1, lon2, lat2), 2)}


@mcp.tool()
@traced_tool
def reverse_geocode(lon: float, lat: float) -> dict:
    """Human-readable description of a location (Nominatim, no key, rate-limited). Falls back to nearest port."""
    if os.getenv("GEO_USE_OSM", "false").lower() == "true":
        try:
            r = httpx.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lon": lon, "lat": lat, "format": "jsonv2", "zoom": 8},
                headers={"User-Agent": "argus/0.2"},
                timeout=15,
            )
            j = r.json()
            return {
                "source": "nominatim",
                "display_name": untrusted(j.get("display_name"), "osm nominatim"),
                "address": untrusted(
                    json.dumps(j.get("address") or {}), "osm nominatim"
                ),
            }
        except Exception as e:  # noqa: BLE001
            return {"source": "nominatim", "error": str(e)}
    p = nearest_ports(lon, lat, 1)["ports"][0]
    return {
        "source": "gazetteer",
        "display_name": f"{p['distance_nm']} nm from {p['name']} ({p['country']})",
    }
