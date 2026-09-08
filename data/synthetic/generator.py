"""Synthetic AIS generator.

Turns a scenario YAML into (a) a list of AIS position reports and (b) a ground-truth
list of injected anomalies used by the evaluation suite. Pure Python, no DB access.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

NM_PER_DEG_LAT = 60.0


@dataclass
class Position:
    mmsi: int
    ts: datetime
    lon: float
    lat: float
    sog: float
    cog: float
    nav_status: str = "under_way"
    source: str = "synthetic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mmsi": self.mmsi,
            "ts": self.ts.isoformat(),
            "lon": round(self.lon, 5),
            "lat": round(self.lat, 5),
            "sog": round(self.sog, 1),
            "cog": round(self.cog, 1),
            "nav_status": self.nav_status,
            "source": self.source,
        }


@dataclass
class GroundTruth:
    mmsi: int
    kind: str
    started_at: datetime
    ended_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        dlon
    )
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _distance_nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[1] + b[1]) / 2)
    dx = (b[0] - a[0]) * NM_PER_DEG_LAT * math.cos(lat)
    dy = (b[1] - a[1]) * NM_PER_DEG_LAT
    return math.hypot(dx, dy)


def _step(
    pos: tuple[float, float], bearing: float, dist_nm: float
) -> tuple[float, float]:
    lat = math.radians(pos[1])
    dlat = dist_nm * math.cos(math.radians(bearing)) / NM_PER_DEG_LAT
    dlon = dist_nm * math.sin(math.radians(bearing)) / (NM_PER_DEG_LAT * math.cos(lat))
    return (pos[0] + dlon, pos[1] + dlat)


class ScenarioGenerator:
    def __init__(self, scenario: dict[str, Any]):
        self.s = scenario
        self.rng = random.Random(scenario.get("seed", 0))
        self.start = datetime.fromisoformat(
            scenario["start_time"].replace("Z", "+00:00")
        )
        self.end = self.start + timedelta(minutes=scenario["duration_minutes"])
        self.dt = timedelta(seconds=scenario.get("interval_seconds", 60))
        self.positions: list[Position] = []
        self.truth: list[GroundTruth] = []
        self._tracks: dict[int, list[Position]] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> ScenarioGenerator:
        with open(path) as f:
            return cls(yaml.safe_load(f))

    # ---- behaviours ------------------------------------------------------
    def _emit(self, p: Position, silent: bool = False) -> None:
        if not silent:
            self.positions.append(p)
        self._tracks.setdefault(p.mmsi, []).append(p)

    def _jitter(self, v: float, amt: float) -> float:
        return v + self.rng.uniform(-amt, amt)

    def _transit(
        self,
        mmsi: int,
        waypoints: list,
        speed: float,
        t: datetime,
        silent=False,
        start: tuple[float, float] | None = None,
        until: datetime | None = None,
    ) -> tuple[datetime, tuple[float, float]]:
        pos = tuple(waypoints[0])
        legs = [tuple(w) for w in waypoints[1:]]
        if start is not None and _distance_nm(start, pos) > 0.1:
            pos, legs = (
                start,
                [tuple(waypoints[0])] + legs,
            )  # continue smoothly from where we are
        stop = until or self.end
        for wp in legs:
            while _distance_nm(pos, wp) > 0.05 and t < stop:
                brg = _bearing(pos, wp)
                d = min(speed * self.dt.total_seconds() / 3600, _distance_nm(pos, wp))
                pos = _step(pos, brg, d)
                self._emit(
                    Position(
                        mmsi,
                        t,
                        pos[0],
                        pos[1],
                        self._jitter(speed, 0.4),
                        self._jitter(brg, 2),
                    ),
                    silent,
                )
                t += self.dt
        return t, pos

    def _anchor(
        self, mmsi: int, at: tuple[float, float], minutes: int, t: datetime
    ) -> tuple[datetime, tuple[float, float]]:
        stop = t + timedelta(minutes=minutes)
        while t < stop and t < self.end:
            self._emit(
                Position(
                    mmsi,
                    t,
                    self._jitter(at[0], 0.0004),
                    self._jitter(at[1], 0.0003),
                    0.1,
                    self.rng.uniform(0, 360),
                    "at_anchor",
                )
            )
            t += self.dt
        return t, at

    def _loiter(
        self,
        mmsi: int,
        center: tuple[float, float],
        radius_nm: float,
        minutes: int,
        speed: float,
        t: datetime,
    ) -> tuple[datetime, tuple[float, float]]:
        stop = t + timedelta(minutes=minutes)
        angle = self.rng.uniform(0, 360)
        pos = _step(center, angle, radius_nm * 0.6)
        while t < stop and t < self.end:
            angle = (angle + self.rng.uniform(15, 60)) % 360
            target = _step(center, angle, self.rng.uniform(0.2, radius_nm))
            brg = _bearing(pos, target)
            pos = _step(pos, brg, speed * self.dt.total_seconds() / 3600)
            self._emit(
                Position(
                    mmsi,
                    t,
                    pos[0],
                    pos[1],
                    self._jitter(speed, 0.3),
                    brg,
                    "restricted_manoeuvrability",
                )
            )
            t += self.dt
        return t, pos

    def _fishing(
        self,
        mmsi: int,
        center: tuple[float, float],
        radius_nm: float,
        speed: float,
        t: datetime,
    ) -> tuple[datetime, tuple[float, float]]:
        pos = center
        leg = 0
        while t < self.end:
            brg = (90 if leg % 2 == 0 else 270) + self.rng.uniform(-8, 8)
            for _ in range(
                int(radius_nm * 2 / max(speed * self.dt.total_seconds() / 3600, 0.01))
            ):
                if t >= self.end:
                    break
                pos = _step(pos, brg, speed * self.dt.total_seconds() / 3600)
                self._emit(
                    Position(
                        mmsi,
                        t,
                        pos[0],
                        pos[1],
                        self._jitter(speed, 0.5),
                        brg,
                        "engaged_in_fishing",
                    )
                )
                t += self.dt
            pos = _step(pos, 0, 0.4)
            leg += 1
        return t, pos

    # ---- driver -----------------------------------------------------------
    def run(self) -> ScenarioGenerator:
        for v in self.s["vessels"]:
            mmsi = v["mmsi"]
            t = self.start
            pos: tuple[float, float] | None = None
            for b in v["behaviours"]:
                kind = b["type"]
                if kind == "transit":
                    t, pos = self._transit(
                        mmsi, b["waypoints"], b["speed_kn"], t, start=pos
                    )
                elif kind == "anchor":
                    t, pos = self._anchor(mmsi, tuple(b["at"]), b["duration_min"], t)
                elif kind == "loiter":
                    t0 = t
                    t, pos = self._loiter(
                        mmsi,
                        tuple(b["center"]),
                        b["radius_nm"],
                        b["duration_min"],
                        b["speed_kn"],
                        t,
                    )
                    self.truth.append(
                        GroundTruth(
                            mmsi,
                            "loitering",
                            t0,
                            t,
                            {"center": b["center"], "radius_nm": b["radius_nm"]},
                        )
                    )
                elif kind == "fishing_pattern":
                    t, pos = self._fishing(
                        mmsi, tuple(b["center"]), b["radius_nm"], b["speed_kn"], t
                    )
                elif kind == "dark_gap":
                    t0 = t
                    # True movement continues but nothing is broadcast.
                    t, pos = self._transit(
                        mmsi,
                        b["drift_waypoints"],
                        b["speed_kn"],
                        t,
                        silent=True,
                        start=pos,
                        until=t0 + timedelta(minutes=b["duration_min"]),
                    )
                    t = max(t, t0 + timedelta(minutes=b["duration_min"]))
                    self.truth.append(
                        GroundTruth(
                            mmsi,
                            "ais_gap",
                            t0,
                            t,
                            {
                                "duration_min": b["duration_min"],
                                "last_known": b["drift_waypoints"][0],
                            },
                        )
                    )
                elif kind == "rendezvous":
                    t0 = t
                    t, pos = self._anchor(mmsi, tuple(b["at"]), b["duration_min"], t)
                    self.truth.append(
                        GroundTruth(
                            mmsi,
                            "rendezvous",
                            t0,
                            t,
                            {"with_mmsi": b["with_mmsi"], "at": b["at"]},
                        )
                    )
                else:
                    raise ValueError(f"unknown behaviour {kind}")
            if v.get("spoof_of"):
                first, last = self._tracks[mmsi][0].ts, self._tracks[mmsi][-1].ts
                self.truth.append(
                    GroundTruth(
                        mmsi,
                        "mmsi_spoof",
                        first,
                        last,
                        {"clone_of": v["spoof_of"], "imo": v.get("imo")},
                    )
                )
        self.positions.sort(key=lambda p: p.ts)
        return self

    # ---- exports ----------------------------------------------------------
    def vessels(self) -> list[dict[str, Any]]:
        seen: dict[int, dict[str, Any]] = {}
        for v in self.s["vessels"]:
            if v["mmsi"] in seen:
                continue
            seen[v["mmsi"]] = {
                k: v.get(k)
                for k in ("mmsi", "imo", "name", "flag", "ship_type", "length_m")
            }
        return list(seen.values())

    def zones(self) -> list[dict[str, Any]]:
        return self.s.get("zones", [])

    def registry(self) -> list[dict[str, Any]]:
        return self.s.get("registry", [])

    def ground_truth(self) -> list[dict[str, Any]]:
        return [
            {
                "mmsi": g.mmsi,
                "kind": g.kind,
                "started_at": g.started_at.isoformat(),
                "ended_at": g.ended_at.isoformat(),
                "details": g.details,
            }
            for g in self.truth
        ]


if __name__ == "__main__":  # quick smoke test
    import json
    import sys

    g = ScenarioGenerator.from_file(
        sys.argv[1] if len(sys.argv) > 1 else "data/scenarios/east_med_baseline.yaml"
    ).run()
    print(
        f"positions={len(g.positions)} vessels={len(g.vessels())} truth={json.dumps(g.ground_truth(), indent=1)}"
    )
