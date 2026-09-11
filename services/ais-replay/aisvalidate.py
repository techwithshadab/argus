"""Validation for live AIS position reports. Pure: no database, no network.

AIS encodes "not available" as out-of-range values: latitude 91, longitude 181,
course 360, speed 102.3. PostGIS rejects those coordinates, and the resulting
`psycopg.DataError` was not caught, so one bad message from the feed crash-looped
the ingest task. Reports at exactly (0, 0) are the other half of the problem: they
are the null island default, not a position off Ghana, and they manufacture false
MMSI spoofs and rendezvous because dozens of unrelated vessels sit on the same point.

`position_record` returns None for anything that must not be stored, so the caller
drops the message instead of failing the batch.
"""

from __future__ import annotations

import os

#: AIS "not available" sentinels and the ranges the database will accept.
LAT_RANGE = (-90.0, 90.0)
LON_RANGE = (-180.0, 180.0)
#: Speed over ground: 102.3 knots means unavailable; anything faster is a decode error.
MAX_SOG = 102.2
#: Course over ground: 360 means unavailable.
MAX_COG = 359.9
#: Reports this close to (0, 0) are the null-island default rather than a fix.
NULL_ISLAND_EPS = 0.0001


def valid_position(lat, lon) -> bool:
    """True when the pair is a fix the database can store and a detector should trust."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:  # NaN
        return False
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        return False
    if not (LON_RANGE[0] <= lon <= LON_RANGE[1]):
        return False
    return abs(lat) > NULL_ISLAND_EPS or abs(lon) > NULL_ISLAND_EPS


def clean_sog(value) -> float:
    """Speed in knots, or 0.0 when the feed says unavailable or sends nonsense."""
    try:
        sog = float(value)
    except (TypeError, ValueError):
        return 0.0
    return sog if 0.0 <= sog <= MAX_SOG else 0.0


def clean_cog(value) -> float:
    """Course in degrees, or 0.0 when the feed says unavailable (360) or sends nonsense."""
    try:
        cog = float(value)
    except (TypeError, ValueError):
        return 0.0
    return cog if 0.0 <= cog <= MAX_COG else 0.0


def position_record(report: dict, ts: str) -> dict | None:
    """A row ready for `positions`, or None when the report must be dropped.

    `report` is AISStream's PositionReport object. An unusable MMSI or coordinate
    pair drops the message; an unusable speed or course is zeroed, because the
    position itself is still worth keeping.
    """
    try:
        mmsi = int(report["UserID"])
    except (KeyError, TypeError, ValueError):
        return None
    if mmsi <= 0:
        return None
    lat, lon = report.get("Latitude"), report.get("Longitude")
    if not valid_position(lat, lon):
        return None
    return {
        "mmsi": mmsi,
        "ts": ts,
        "lon": float(lon),
        "lat": float(lat),
        "sog": clean_sog(report.get("Sog", 0.0)),
        "cog": clean_cog(report.get("Cog", 0.0)),
        "nav_status": str(report.get("NavigationalStatus")),
        "source": "aisstream",
    }


#: Positions written per round trip, and the longest a report waits for its batch.
BATCH_MAX = int(os.getenv("AIS_BATCH_MAX", "200"))
BATCH_MAX_S = float(os.getenv("AIS_BATCH_MAX_S", "2"))


def should_flush(
    pending: int, waited_s: float, size: int = 0, age_s: float = 0.0
) -> bool:
    """Whether a batch of `pending` reports that has waited `waited_s` should be written.

    One insert per message was three autocommitted round trips each: a vessels upsert,
    a positions insert and a Redis publish. At live rates that is thousands of commits
    a minute, and because the receive loop awaits each one, a slow database grew the
    websocket buffer until the server dropped the connection, which then looked exactly
    like a feed outage in the logs (P15).
    """
    limit = size or BATCH_MAX
    window = age_s or BATCH_MAX_S
    return pending >= limit or (pending > 0 and waited_s >= window)


def position_rows(batch: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """`(vessel rows, position rows)` for one batch, in the order the inserts take them."""
    vessels = [(b["mmsi"], b.get("name") or "") for b in batch]
    positions = [
        (
            b["mmsi"],
            b["ts"],
            f"SRID=4326;POINT({b['lon']} {b['lat']})",
            b["sog"],
            b["cog"],
            b["nav_status"],
            b["source"],
        )
        for b in batch
    ]
    return vessels, positions
