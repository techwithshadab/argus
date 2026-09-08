"""Imagery MCP server.

search_sentinel_scenes queries the Copernicus Data Space catalogue (OData, no auth required
for search) for Sentinel-1 SAR and Sentinel-2 optical scenes covering a point. Everything
else (tasking requests) is written to the database for a human to approve."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
from common.db import q
from common.telemetry import traced_tool
from mcp.server.fastmcp import FastMCP
from psycopg.types.json import Jsonb

mcp = FastMCP(
    "imagery",
    instructions="Search satellite imagery archives and propose collection (re-look) tasking for human approval.",
    stateless_http=True,
    json_response=True,
)

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
OFFLINE = os.getenv("IMAGERY_OFFLINE", "false").lower() == "true"


def _caller_role() -> str:
    """IAM role of the verified caller (set by CallerAuthMiddleware), else the agent's default name."""
    try:
        caller = mcp.get_context().request_context.request.state.caller
        return caller.role if caller.kind == "agent" else "tasking-agent"
    except Exception:  # noqa: BLE001
        return "tasking-agent"


@mcp.tool()
@traced_tool
def search_sentinel_scenes(
    lon: float,
    lat: float,
    start: str,
    end: str,
    collection: str = "SENTINEL-1",
    max_results: int = 5,
) -> dict:
    """Find archived Sentinel scenes intersecting a point between `start` and `end` (ISO dates).
    collection: SENTINEL-1 (SAR, sees through cloud and darkness) or SENTINEL-2 (optical)."""
    if OFFLINE:
        return _synthetic_scenes(lon, lat, start, end, collection, max_results)
    flt = (
        f"Collection/Name eq '{collection}' and OData.CSC.Intersects(area=geography'SRID=4326;POINT({lon} {lat})') "
        f"and ContentDate/Start gt {start[:10]}T00:00:00.000Z and ContentDate/Start lt {end[:10]}T23:59:59.000Z"
    )
    try:
        r = httpx.get(
            CATALOGUE,
            params={
                "$filter": flt,
                "$top": max_results,
                "$orderby": "ContentDate/Start desc",
            },
            timeout=30,
        )
        r.raise_for_status()
        scenes = [
            {
                "id": p["Id"],
                "name": p["Name"],
                "sensed": p["ContentDate"]["Start"],
                "size_mb": round(p.get("ContentLength", 0) / 1e6, 1),
                "browse_url": f"https://browser.dataspace.copernicus.eu/?zoom=9&lat={lat}&lng={lon}",
            }
            for p in r.json().get("value", [])
        ]
        return {
            "source": "copernicus-dataspace",
            "collection": collection,
            "scenes": scenes,
            "count": len(scenes),
        }
    except Exception as e:  # noqa: BLE001
        out = _synthetic_scenes(lon, lat, start, end, collection, max_results)
        out["warning"] = (
            f"catalogue unreachable ({e}); returning a placeholder scene list"
        )
        return out


def _synthetic_scenes(lon, lat, start, end, collection, n):
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    step = 6 if collection == "SENTINEL-1" else 5
    scenes = [
        {
            "id": f"synthetic-{collection}-{i}",
            "name": f"{collection}_SYNTH_{(t0 + timedelta(days=i * step)).date()}",
            "sensed": (t0 + timedelta(days=i * step)).isoformat(),
            "size_mb": 850.0,
        }
        for i in range(n)
    ]
    return {
        "source": "synthetic",
        "collection": collection,
        "scenes": scenes,
        "count": len(scenes),
    }


@mcp.tool()
@traced_tool
def estimate_next_pass(lon: float, lat: float, collection: str = "SENTINEL-1") -> dict:
    """Rough estimate of the next Sentinel pass over a point, from the most recent archived scene plus the nominal revisit period.
    This is an estimate for planning, not an orbital prediction."""
    end = datetime.now(UTC)
    recent = search_sentinel_scenes(
        lon, lat, (end - timedelta(days=20)).isoformat(), end.isoformat(), collection, 1
    )
    revisit_days = 6 if collection == "SENTINEL-1" else 5
    if recent["scenes"]:
        last = datetime.fromisoformat(
            recent["scenes"][0]["sensed"].replace("Z", "+00:00")
        )
        nxt = last
        while nxt <= end:
            nxt += timedelta(days=revisit_days)
        return {
            "collection": collection,
            "last_scene": last.isoformat(),
            "estimated_next_pass": nxt.isoformat(),
            "revisit_days": revisit_days,
            "confidence": "low",
        }
    return {
        "collection": collection,
        "estimated_next_pass": (end + timedelta(days=revisit_days / 2)).isoformat(),
        "revisit_days": revisit_days,
        "confidence": "very_low",
    }


@mcp.tool()
@traced_tool
def create_tasking_request(
    mmsi: int,
    sensor: str,
    center_lon: float,
    center_lat: float,
    radius_nm: float,
    window_start: str,
    window_end: str,
    rationale: str,
    priority: str = "routine",
) -> dict:
    """Propose a collection request (SAR re-look, optical, patrol aircraft) for HUMAN APPROVAL. Creates a record with status 'proposed'.
    sensor: sentinel-1-sar | sentinel-2-optical | commercial-sar | patrol-aircraft."""
    d = radius_nm / 60.0
    ring = [
        (center_lon - d, center_lat - d),
        (center_lon + d, center_lat - d),
        (center_lon + d, center_lat + d),
        (center_lon - d, center_lat + d),
        (center_lon - d, center_lat - d),
    ]
    wkt = "POLYGON((" + ",".join(f"{x} {y}" for x, y in ring) + "))"
    rows = q(
        """INSERT INTO tasking_requests (mmsi, sensor, aoi, window_start, window_end, rationale, priority)
           VALUES (%s,%s,ST_GeogFromText(%s),%s,%s,%s,%s) RETURNING id, status, created_at""",
        (mmsi, sensor, wkt, window_start, window_end, rationale, priority),
    )
    r = rows[0]
    q(
        """INSERT INTO audit_events (actor, actor_kind, action, entity_kind, entity_id, details)
           VALUES (%s, 'agent', 'tasking.proposed', 'tasking_request', %s, %s)""",
        (
            _caller_role(),
            str(r["id"]),
            Jsonb({"mmsi": mmsi, "sensor": sensor, "priority": priority}),
        ),
    )
    return {
        "tasking_id": str(r["id"]),
        "status": r["status"],
        "created_at": r["created_at"].isoformat(),
        "note": "Awaiting human approval in the UI.",
    }


@mcp.tool()
@traced_tool
def list_tasking_requests(mmsi: int | None = None) -> dict:
    """Existing tasking requests, to avoid duplicates."""
    rows = q(
        "SELECT id, mmsi, sensor, window_start, window_end, priority, status, rationale FROM tasking_requests WHERE (%s::bigint IS NULL OR mmsi=%s) ORDER BY created_at DESC",
        (mmsi, mmsi),
    )
    return {
        "requests": [
            {
                **r,
                "id": str(r["id"]),
                "window_start": r["window_start"].isoformat()
                if r["window_start"]
                else None,
                "window_end": r["window_end"].isoformat() if r["window_end"] else None,
            }
            for r in rows
        ]
    }
