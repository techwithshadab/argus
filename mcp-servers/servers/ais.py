"""AIS MCP server: track retrieval and anomaly primitives over PostGIS.

Every tool returns plain JSON. Anomaly tools are deliberately deterministic so the watch
agent reasons over evidence rather than inventing it."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from common.db import q
from common.safety import untrusted
from common.telemetry import traced_tool
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "ais",
    instructions="Tools for querying vessel AIS tracks and detecting AIS gaps, MMSI spoofing, loitering, rendezvous and zone incursions.",
    stateless_http=True,
    json_response=True,
)

SCENARIO_END = os.getenv("SCENARIO_END", "2026-09-01T08:00:00Z")


_FEED_TEXT = ("name", "name_a", "name_b")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    """Query rows with every feed-derived free-text column marked as untrusted. Vessel
    names come from the AIS static feed (an unauthenticated broadcast) and reach the model
    on almost every tool call, so they are wrapped here, at the boundary, not per tool."""
    out = []
    for r in q(sql, params):
        r = dict(r)
        for k in _FEED_TEXT:
            if r.get(k):
                r[k] = untrusted(r[k], "ais static")
        out.append(r)
    return out


def scenario_now() -> str:
    """The clock every window is measured from: the scenario's end in replay, the wall clock
    in live mode (AIS_MODE=live), as ISO-8601 UTC."""
    if os.getenv("AIS_MODE", "replay").strip().lower() == "live":
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SCENARIO_END


def _window(hours: float, until: str | None) -> tuple[datetime, datetime]:
    end = datetime.fromisoformat((until or scenario_now()).replace("Z", "+00:00"))
    return end - timedelta(hours=hours), end


@mcp.tool()
@traced_tool
def get_vessel_track(
    mmsi: int, hours: float = 24, until: str | None = None, max_points: int = 120
) -> dict:
    """Return the AIS track (time-ordered positions) for a vessel over the last `hours` before `until` (ISO time, defaults to scenario end). Downsampled to `max_points` (default 120; a sweep that reads several tracks must fit the model context)."""
    start, end = _window(hours, until)
    rows = _rows(
        """SELECT ts, ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon, sog, cog, nav_status
           FROM positions WHERE mmsi=%s AND ts BETWEEN %s AND %s ORDER BY ts""",
        (mmsi, start, end),
    )
    total = len(rows)
    step = max(1, -(-total // max(1, max_points)))
    rows = rows[::step]
    return {
        "mmsi": mmsi,
        "points": len(rows),
        "points_total": total,
        "track": [{**r, "ts": r["ts"].isoformat()} for r in rows],
    }


@mcp.tool()
@traced_tool
def get_latest_position(mmsi: int) -> dict:
    """Latest known position and static data for a vessel."""
    rows = _rows("SELECT * FROM latest_positions WHERE mmsi=%s", (mmsi,))
    if not rows:
        return {"error": f"no positions for mmsi {mmsi}"}
    r = rows[0]
    r["ts"] = r["ts"].isoformat()
    return r


@mcp.tool()
@traced_tool
def list_vessels(hours: float = 24, until: str | None = None) -> dict:
    """List vessels that reported at least one position in the window, with their latest position."""
    start, end = _window(hours, until)
    rows = _rows(
        """SELECT DISTINCT ON (p.mmsi) p.mmsi, v.name, v.flag, v.ship_type, p.ts,
                  ST_Y(p.geom::geometry) lat, ST_X(p.geom::geometry) lon, p.sog, p.nav_status
           FROM positions p LEFT JOIN vessels v USING (mmsi)
           WHERE p.ts BETWEEN %s AND %s ORDER BY p.mmsi, p.ts DESC""",
        (start, end),
    )
    return {
        "count": len(rows),
        "vessels": [{**r, "ts": r["ts"].isoformat()} for r in rows],
    }


@mcp.tool()
@traced_tool
def find_ais_gaps(
    mmsi: int | None = None,
    min_gap_minutes: int = 30,
    hours: float = 24,
    until: str | None = None,
) -> dict:
    """Find periods where a vessel stopped transmitting for at least `min_gap_minutes`.
    Includes the last position before the gap, the first after it, and the implied distance and speed."""
    start, end = _window(hours, until)
    rows = _rows(
        """WITH s AS (
             SELECT mmsi, ts, geom,
                    LAG(ts) OVER (PARTITION BY mmsi ORDER BY ts) prev_ts,
                    LAG(geom) OVER (PARTITION BY mmsi ORDER BY ts) prev_geom
             FROM positions WHERE ts BETWEEN %s AND %s AND (%s::bigint IS NULL OR mmsi=%s))
           SELECT s.mmsi, v.name, prev_ts AS gap_start, ts AS gap_end,
                  EXTRACT(EPOCH FROM (ts - prev_ts))/60 AS gap_minutes,
                  ST_Y(prev_geom::geometry) last_lat, ST_X(prev_geom::geometry) last_lon,
                  ST_Y(geom::geometry) next_lat, ST_X(geom::geometry) next_lon,
                  ST_Distance(prev_geom, geom)/1852.0 AS distance_nm
           FROM s LEFT JOIN vessels v USING (mmsi)
           WHERE prev_ts IS NOT NULL AND ts - prev_ts >= make_interval(mins => %s)
           ORDER BY gap_minutes DESC""",
        (start, end, mmsi, mmsi, min_gap_minutes),
    )
    # Also report vessels whose last report is well before the window end (still dark).
    still_dark = _rows(
        """SELECT l.mmsi, l.name, l.ts AS gap_start, l.lat last_lat, l.lon last_lon,
                  EXTRACT(EPOCH FROM (%s::timestamptz - l.ts))/60 AS gap_minutes
           FROM latest_positions l WHERE l.ts < %s::timestamptz - make_interval(mins => %s)
                 AND (%s::bigint IS NULL OR l.mmsi=%s)""",
        (end, end, min_gap_minutes, mmsi, mmsi),
    )

    def fmt(r: dict) -> dict:
        return {
            k: (
                v.isoformat()
                if isinstance(v, datetime)
                else (float(v) if hasattr(v, "quantize") else v)
            )
            for k, v in r.items()
        }

    gaps = [fmt(r) for r in rows]
    for g in gaps:
        if g.get("gap_minutes"):
            g["implied_speed_kn"] = round(g["distance_nm"] / (g["gap_minutes"] / 60), 1)
    return {
        "window": [start.isoformat(), end.isoformat()],
        "gaps": gaps,
        "still_dark": [fmt(r) for r in still_dark],
    }


@mcp.tool()
@traced_tool
def detect_mmsi_conflicts(
    max_plausible_speed_kn: float = 40, hours: float = 24, until: str | None = None
) -> dict:
    """Detect a single MMSI being broadcast from two places at once (identity spoofing):
    consecutive reports whose implied speed exceeds `max_plausible_speed_kn`."""
    start, end = _window(hours, until)
    rows = _rows(
        """WITH s AS (
             SELECT mmsi, ts, geom, LAG(ts) OVER w prev_ts, LAG(geom) OVER w prev_geom
             FROM positions WHERE ts BETWEEN %s AND %s WINDOW w AS (PARTITION BY mmsi ORDER BY ts))
           SELECT mmsi, COUNT(*) AS conflicting_reports, MIN(ts) first_seen, MAX(ts) last_seen,
                  MAX(ST_Distance(prev_geom, geom)/1852.0) AS max_jump_nm
           FROM s WHERE prev_ts IS NOT NULL AND ts > prev_ts
                 AND (ST_Distance(prev_geom, geom)/1852.0) / GREATEST(EXTRACT(EPOCH FROM (ts-prev_ts))/3600, 0.001) > %s
           GROUP BY mmsi ORDER BY conflicting_reports DESC""",
        (start, end, max_plausible_speed_kn),
    )
    out = []
    for r in rows:
        # Split the reports into two spatial clusters so the caller can see both tracks.
        pts = q(
            """SELECT ts, ST_Y(geom::geometry) lat, ST_X(geom::geometry) lon FROM positions
               WHERE mmsi=%s AND ts BETWEEN %s AND %s ORDER BY ts""",
            (r["mmsi"], start, end),
        )
        clusters = _two_means([(p["lon"], p["lat"]) for p in pts])
        out.append(
            {
                "mmsi": r["mmsi"],
                "conflicting_reports": r["conflicting_reports"],
                "first_seen": r["first_seen"].isoformat(),
                "last_seen": r["last_seen"].isoformat(),
                "max_jump_nm": round(float(r["max_jump_nm"]), 1),
                "track_centroids": clusters,
            }
        )
    return {"conflicts": out}


def _two_means(points: list[tuple[float, float]]) -> list[dict]:
    if len(points) < 4:
        return []
    a, b = points[0], points[-1]
    for _ in range(8):
        ga = [
            p
            for p in points
            if (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2
            <= (p[0] - b[0]) ** 2 + (p[1] - b[1]) ** 2
        ]
        gb = [p for p in points if p not in ga]
        if not ga or not gb:
            break
        a = (sum(p[0] for p in ga) / len(ga), sum(p[1] for p in ga) / len(ga))
        b = (sum(p[0] for p in gb) / len(gb), sum(p[1] for p in gb) / len(gb))
    return [
        {"lon": round(a[0], 3), "lat": round(a[1], 3), "reports": len(ga)},
        {"lon": round(b[0], 3), "lat": round(b[1], 3), "reports": len(gb)},
    ]


@mcp.tool()
@traced_tool
def detect_loitering(
    min_duration_minutes: int = 90,
    max_radius_nm: float = 2.0,
    max_speed_kn: float = 3.0,
    hours: float = 24,
    until: str | None = None,
) -> dict:
    """Find vessels that stayed within `max_radius_nm` for at least `min_duration_minutes` at low speed,
    excluding vessels reporting at_anchor inside a declared anchorage. Reports which zones the loiter box intersects."""
    start, end = _window(hours, until)
    rows = _rows(
        """WITH slow AS (
             SELECT mmsi, ts, geom FROM positions
             WHERE ts BETWEEN %s AND %s AND sog <= %s AND nav_status <> 'at_anchor'),
           agg AS (
             SELECT mmsi, MIN(ts) started_at, MAX(ts) ended_at, COUNT(*) reports,
                    ST_Centroid(ST_Collect(geom::geometry)) c,
                    ST_MaxDistance(ST_Collect(geom::geometry), ST_Collect(geom::geometry)) span_deg
             FROM slow GROUP BY mmsi)
           SELECT a.mmsi, v.name, v.ship_type, started_at, ended_at,
                  EXTRACT(EPOCH FROM (ended_at-started_at))/60 duration_minutes, reports,
                  ST_Y(c) lat, ST_X(c) lon, span_deg*60 AS span_nm_approx,
                  (SELECT json_agg(json_build_object('name', z.name, 'kind', z.kind))
                     FROM zones z WHERE ST_Intersects(z.geom, ST_SetSRID(c,4326)::geography)) AS zones
           FROM agg a LEFT JOIN vessels v USING (mmsi)
           WHERE EXTRACT(EPOCH FROM (ended_at-started_at))/60 >= %s AND span_deg*60 <= %s*2
           ORDER BY duration_minutes DESC""",
        (start, end, max_speed_kn, min_duration_minutes, max_radius_nm),
    )
    return {
        "loitering": [
            {
                **r,
                "started_at": r["started_at"].isoformat(),
                "ended_at": r["ended_at"].isoformat(),
                "duration_minutes": round(float(r["duration_minutes"])),
                "span_nm_approx": round(float(r["span_nm_approx"]), 2),
            }
            for r in rows
        ]
    }


@mcp.tool()
@traced_tool
def detect_rendezvous(
    max_separation_nm: float = 0.5,
    min_minutes: int = 30,  # the detector evals showed a scripted 90-min rendezvous overlaps at low speed for only ~40 min
    hours: float = 24,
    until: str | None = None,
) -> dict:
    """Find pairs of vessels that remained within `max_separation_nm` of each other for at least `min_minutes`
    (possible ship-to-ship transfer)."""
    start, end = _window(hours, until)
    rows = _rows(
        """SELECT a.mmsi mmsi_a, b.mmsi mmsi_b, va.name name_a, vb.name name_b,
                  MIN(a.ts) started_at, MAX(a.ts) ended_at, COUNT(*) samples,
                  ST_Y(ST_Centroid(ST_Collect(a.geom::geometry))) lat, ST_X(ST_Centroid(ST_Collect(a.geom::geometry))) lon,
                  mode() WITHIN GROUP (ORDER BY a.nav_status) nav_status_a,
                  mode() WITHIN GROUP (ORDER BY b.nav_status) nav_status_b,
                  (SELECT json_agg(json_build_object('name', z.name, 'kind', z.kind))
                     FROM zones z WHERE ST_Intersects(z.geom, ST_SetSRID(ST_Centroid(ST_Collect(a.geom::geometry)),4326)::geography)) AS zones
           FROM positions a JOIN positions b ON a.mmsi < b.mmsi AND a.ts = b.ts
                AND ST_DWithin(a.geom, b.geom, %s*1852)
           LEFT JOIN vessels va ON va.mmsi=a.mmsi LEFT JOIN vessels vb ON vb.mmsi=b.mmsi
           WHERE a.ts BETWEEN %s AND %s AND a.sog < 2 AND b.sog < 2
           GROUP BY a.mmsi, b.mmsi, va.name, vb.name
           HAVING EXTRACT(EPOCH FROM (MAX(a.ts)-MIN(a.ts)))/60 >= %s""",
        (max_separation_nm, start, end, min_minutes),
    )
    return {
        "rendezvous": [
            {
                **r,
                "started_at": r["started_at"].isoformat(),
                "ended_at": r["ended_at"].isoformat(),
            }
            for r in rows
        ]
    }


@mcp.tool()
@traced_tool
def list_zone_incursions(
    kinds: list[str] | None = None, hours: float = 24, until: str | None = None
) -> dict:
    """List vessels whose positions fell inside zones of the given kinds (default: exclusion and protected_cable)."""
    kinds = kinds or ["exclusion", "protected_cable"]
    start, end = _window(hours, until)
    rows = _rows(
        """SELECT p.mmsi, v.name, z.name zone, z.kind, MIN(p.ts) first_inside, MAX(p.ts) last_inside, COUNT(*) reports,
                  AVG(p.sog) avg_sog
           FROM positions p JOIN zones z ON ST_Intersects(p.geom, z.geom)
           LEFT JOIN vessels v USING (mmsi)
           WHERE p.ts BETWEEN %s AND %s AND z.kind = ANY(%s)
           GROUP BY p.mmsi, v.name, z.name, z.kind ORDER BY reports DESC""",
        (start, end, kinds),
    )
    return {
        "incursions": [
            {
                **r,
                "first_inside": r["first_inside"].isoformat(),
                "last_inside": r["last_inside"].isoformat(),
                "avg_sog": round(float(r["avg_sog"] or 0), 1),
            }
            for r in rows
        ]
    }


@mcp.tool()
@traced_tool
def find_vessels_near(
    lon: float,
    lat: float,
    radius_nm: float = 10,
    at_time: str | None = None,
    tolerance_minutes: int = 30,
) -> dict:
    """Vessels that reported within `radius_nm` of a point around `at_time` (ISO). Useful for 'who was nearby'."""
    t = datetime.fromisoformat((at_time or scenario_now()).replace("Z", "+00:00"))
    rows = _rows(
        """SELECT DISTINCT ON (p.mmsi) p.mmsi, v.name, v.ship_type, p.ts,
                  ST_Distance(p.geom, ST_MakePoint(%s,%s)::geography)/1852 distance_nm, p.sog
           FROM positions p LEFT JOIN vessels v USING (mmsi)
           WHERE p.ts BETWEEN %s AND %s AND ST_DWithin(p.geom, ST_MakePoint(%s,%s)::geography, %s*1852)
           ORDER BY p.mmsi, ABS(EXTRACT(EPOCH FROM (p.ts - %s)))""",
        (
            lon,
            lat,
            t - timedelta(minutes=tolerance_minutes),
            t + timedelta(minutes=tolerance_minutes),
            lon,
            lat,
            radius_nm,
            t,
        ),
    )
    return {
        "near": [
            {
                **r,
                "ts": r["ts"].isoformat(),
                "distance_nm": round(float(r["distance_nm"]), 2),
            }
            for r in rows
        ]
    }


@mcp.tool()
@traced_tool
def list_open_alerts(mmsi: int | None = None) -> dict:
    """Alerts already raised by the watch agent, so agents do not duplicate work."""
    rows = _rows(
        "SELECT id, mmsi, kind, severity, score, started_at, ended_at, status, details FROM alerts WHERE status='open' AND (%s::bigint IS NULL OR mmsi=%s) ORDER BY created_at DESC",
        (mmsi, mmsi),
    )
    return {
        "alerts": [
            {
                **r,
                "id": str(r["id"]),
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
            }
            for r in rows
        ]
    }
