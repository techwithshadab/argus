"""Deterministic pre-pass for the Watch agent's sweep (pure, unit-tested).

The five AIS detectors are SQL; what they find is not a matter of judgement. The model's
job is to weigh each candidate (severity, explanation, dismissal), not to copy vessel ids
and time windows between tool outputs, which is exactly where a small model slipped: a gap
window from one hull was raised under another. So the candidates, their windows and their
evidence are built here from the detector outputs, deduplicated against alerts that are
already open, and the model refers to them by id."""

from __future__ import annotations

import secrets
from datetime import datetime

from .graph import normalise_kind

KIND_ORDER = ("mmsi_spoof", "ais_gap", "rendezvous", "loitering", "zone_incursion")
DEFAULT_MAX_CANDIDATES = 25


def rank_candidates(
    cands: list[dict], limit: int = DEFAULT_MAX_CANDIDATES
) -> tuple[list[dict], list[dict]]:
    """The candidates worth the model's attention this sweep, most suspicious first, and
    the rest (deferred: they come back next sweep if still detected and not yet open).
    Live AIS is patchy, so a two-hour window can hold a hundred short gaps; the officer's
    attention and the sweep's time budget are the scarce resources."""

    def score(c: dict) -> float:
        base = {
            "mmsi_spoof": 5,
            "rendezvous": 4,
            "ais_gap": 3,
            "loitering": 2,
            "zone_incursion": 2,
        }
        pts = float(base.get(c["kind"], 1))
        text = (c.get("summary") or "").lower()
        if "inside" in text and "no declared" not in text:
            pts += 2  # inside a declared zone or corridor
        if "still dark" in text:
            pts += 1
        if c["kind"] == "ais_gap":
            try:
                minutes = float(text.split("-minute")[0].split()[-1])
                pts += min(minutes / 60, 1.5)  # a long gap counts, never above spoofing
            except (ValueError, IndexError):
                pass
        return pts

    ordered = sorted(
        cands, key=lambda c: (-score(c), KIND_ORDER.index(c["kind"]), c["mmsi"])
    )
    return ordered[:limit], ordered[limit:]


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _overlap(a0, a1, b0, b1) -> bool:
    def f(s):
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))

    try:
        return f(a0) <= f(b1) and f(b0) <= f(a1)
    except (TypeError, ValueError):
        return True  # an alert without a window blocks by (mmsi, kind) alone


def candidates_from(
    detectors: dict, open_alerts: list[dict] | None = None
) -> list[dict]:
    """Candidates from the detector outputs, keyed `<token>-c<n>` so two concurrent sweeps
    never share ids. `detectors` maps `gaps`, `conflicts`, `loitering`, `rendezvous`,
    `incursions` to the raw tool outputs (any may be missing)."""
    out: list[dict] = []
    gaps = detectors.get("gaps") or {}
    window_end = (gaps.get("window") or [None, None])[1]
    for g in gaps.get("gaps") or []:
        out.append(
            {
                "kind": "ais_gap",
                "mmsi": int(g["mmsi"]),
                "name": g.get("name"),
                "started_at": _iso(g.get("gap_start")),
                "ended_at": _iso(g.get("gap_end")),
                "summary": (
                    f"{float(g.get('gap_minutes') or 0):.0f}-minute AIS gap, "
                    f"{float(g.get('distance_nm') or 0):.1f} nm between last and next report"
                    + (
                        f", implied {g['implied_speed_kn']} kn"
                        if g.get("implied_speed_kn") is not None
                        else ""
                    )
                    + f"; last known {g.get('last_lat')},{g.get('last_lon')}"
                ),
                "source": "ais.find_ais_gaps",
                "lat": g.get("last_lat"),
                "lon": g.get("last_lon"),
            }
        )
    for g in gaps.get("still_dark") or []:
        out.append(
            {
                "kind": "ais_gap",
                "mmsi": int(g["mmsi"]),
                "name": g.get("name"),
                "started_at": _iso(g.get("gap_start")),
                "ended_at": _iso(window_end) or _iso(g.get("gap_start")),
                "summary": (
                    f"still dark: no report for {float(g.get('gap_minutes') or 0):.0f} "
                    f"minutes; last known {g.get('last_lat')},{g.get('last_lon')}"
                ),
                "source": "ais.find_ais_gaps",
                "lat": g.get("last_lat"),
                "lon": g.get("last_lon"),
            }
        )
    for c in (detectors.get("conflicts") or {}).get("conflicts") or []:
        out.append(
            {
                "kind": "mmsi_spoof",
                "mmsi": int(c["mmsi"]),
                "name": c.get("name"),
                "started_at": _iso(c.get("first_seen")),
                "ended_at": _iso(c.get("last_seen")),
                "summary": (
                    f"{c.get('conflicting_reports')} reports imply impossible speed, "
                    f"max jump {c.get('max_jump_nm')} nm; two track centroids "
                    f"{c.get('track_centroids')}"
                ),
                "source": "ais.detect_mmsi_conflicts",
            }
        )
    for r in (detectors.get("loitering") or {}).get("loitering") or []:
        zones = ", ".join(z.get("name", "") for z in (r.get("zones") or []) if z)
        out.append(
            {
                "kind": "loitering",
                "mmsi": int(r["mmsi"]),
                "name": r.get("name"),
                "started_at": _iso(r.get("started_at")),
                "ended_at": _iso(r.get("ended_at")),
                "summary": (
                    f"{r.get('duration_minutes')} minutes within "
                    f"{r.get('span_nm_approx')} nm at low speed"
                    + (
                        f", ship type {r.get('ship_type')}"
                        if r.get("ship_type")
                        else ""
                    )
                    + (f", inside {zones}" if zones else ", no declared zone")
                ),
                "source": "ais.detect_loitering",
                "lat": r.get("lat"),
                "lon": r.get("lon"),
            }
        )
    for r in (detectors.get("rendezvous") or {}).get("rendezvous") or []:
        out.append(
            {
                "kind": "rendezvous",
                "mmsi": int(r["mmsi_a"]),
                "name": r.get("name_a"),
                "partner_mmsi": int(r["mmsi_b"]),
                "partner_name": r.get("name_b"),
                "started_at": _iso(r.get("started_at")),
                "ended_at": _iso(r.get("ended_at")),
                "summary": (
                    f"within 0.5 nm of {r.get('name_b') or r.get('mmsi_b')} "
                    f"(MMSI {r.get('mmsi_b')}) for {r.get('samples')} samples at "
                    f"{r.get('lat')},{r.get('lon')}; nav status {r.get('nav_status_a')} "
                    f"/ {r.get('nav_status_b')}; "
                    + (
                        "inside "
                        + ", ".join(
                            f"{z.get('name')} ({z.get('kind')})"
                            for z in (r.get("zones") or [])
                            if z
                        )
                        if r.get("zones")
                        else "no declared zone or anchorage at the meeting point"
                    )
                ),
                "source": "ais.detect_rendezvous",
                "lat": r.get("lat"),
                "lon": r.get("lon"),
            }
        )
    for r in (detectors.get("incursions") or {}).get("incursions") or []:
        out.append(
            {
                "kind": "zone_incursion",
                "mmsi": int(r["mmsi"]),
                "name": r.get("name"),
                "started_at": _iso(r.get("first_inside")),
                "ended_at": _iso(r.get("last_inside")),
                "summary": (
                    f"{r.get('reports')} reports inside {r.get('zone')} ({r.get('kind')}), "
                    f"average {r.get('avg_sog')} kn"
                ),
                "source": "ais.list_zone_incursions",
                "lat": r.get("lat"),
                "lon": r.get("lon"),
            }
        )
    out.sort(
        key=lambda c: (KIND_ORDER.index(c["kind"]), c["mmsi"], c["started_at"] or "")
    )
    kept = [c for c in out if not _already_open(c, open_alerts or [])]
    token = secrets.token_hex(2)
    for i, c in enumerate(kept, 1):
        c["id"] = f"{token}-c{i}"
        c["evidence"] = [
            {
                "source": c["source"],
                "summary": c["summary"],
                "reference": f"{c['kind']} {c['mmsi']} {c['started_at']}",
            }
        ]
    return kept


def _already_open(c: dict, open_alerts: list[dict]) -> bool:
    for a in open_alerts:
        if int(a.get("mmsi") or 0) != c["mmsi"]:
            continue
        if normalise_kind(a.get("kind") or "") != c["kind"]:
            continue
        if _overlap(
            c["started_at"], c["ended_at"], a.get("started_at"), a.get("ended_at")
        ):
            return True
    return False


def render_candidates(cands: list[dict]) -> list[dict]:
    """The compact view the model reasons over."""
    return [
        {
            "id": c["id"],
            "kind": c["kind"],
            "mmsi": c["mmsi"],
            "name": c.get("name"),
            "window": [c["started_at"], c["ended_at"]],
            "summary": c["summary"],
            # The prompt asks the model to call `point_in_zones`, which needs a
            # position. Loitering, rendezvous and incursion candidates carried none, so
            # the model either skipped the zone check or invented coordinates (A5).
            **(
                {"lat": round(float(c["lat"]), 4), "lon": round(float(c["lon"]), 4)}
                if c.get("lat") is not None and c.get("lon") is not None
                else {}
            ),
            **({"partner_mmsi": c["partner_mmsi"]} if c.get("partner_mmsi") else {}),
        }
        for c in cands
    ]


def non_dismissible(c: dict) -> str | None:
    """Why a candidate may only be raised, never dismissed, or None. The reasons are the
    definitions of the anomalies themselves, so they are code, not judgement: one MMSI from
    two places is never benign, and two vessels stopped together outside any declared
    anchorage or port is the ship-to-ship transfer signature the alert exists for."""
    if c["kind"] == "mmsi_spoof":
        return "an MMSI broadcast from two places is never explainable; raise it"
    if c["kind"] == "rendezvous" and "no declared zone" in (c.get("summary") or ""):
        return (
            "a rendezvous outside any declared anchorage or port zone is the ship-to-ship "
            "transfer signature; raise it with the severity you judge"
        )
    return None


def alert_evidence(candidate: dict, extra: list | None) -> list[dict]:
    """The evidence list for an alert: the candidate's own entry first, then whatever the
    model added, canonicalised and cleaned.

    The model sees tools under the name its transport uses. Through the AgentCore gateway
    that is `geo___point_in_zones`, so alerts on AWS cited a form no officer recognises and
    no scorer keyed on `server.tool` could match, while the same sweep run locally cited the
    dotted form. Canonicalising here, rather than in the tool wrapper, keeps it pure and
    testable, and lets malformed entries be dropped instead of stored.
    """
    from .tools import canonical_source

    out = list(candidate.get("evidence") or [])
    seen = {(e.get("source"), e.get("reference")) for e in out if isinstance(e, dict)}
    for e in extra or []:
        if not isinstance(e, dict):
            continue
        source = canonical_source(str(e.get("source") or "").strip())
        summary = str(e.get("summary") or "").strip()
        if not source or not summary:
            # An entry without a source or a summary cites nothing an officer can follow.
            continue
        reference = e.get("reference")
        reference = str(reference).strip() if reference else None
        key = (source, reference)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"source": source, "summary": summary}
            | ({"reference": reference} if reference else {})
        )
    return out


#: The detectors a sweep depends on. `open_alerts` is a sixth call and its failure
#: only disables dedup, so it is not one of these.
DETECTOR_KEYS = ("gaps", "conflicts", "loitering", "rendezvous", "incursions")
#: Candidate entries older than this are dropped from the Watch runtime's caches.
CANDIDATE_TTL_S = 3600.0


def sweep_failure(failed: list[str]) -> str | None:
    """Why this sweep must fail rather than report nothing, or None.

    A denied Cedar policy, a SQL error or a gateway refusal came back as an error
    result that nothing read, so the detector's output became `{}` and the sweep
    reported zero candidates exactly as a quiet watch does. Every detector failing is
    a broken sweep, not a calm sea, and it must fail loudly so the job is retried and
    the alarm fires (A4).
    """
    broken = [k for k in failed if k in DETECTOR_KEYS]
    if len(broken) == len(DETECTOR_KEYS):
        return "every detector failed: " + ", ".join(sorted(broken))
    return None


def expired_candidates(
    stamps: dict[str, float], now: float, ttl: float = CANDIDATE_TTL_S
) -> list[str]:
    """Candidate ids stamped more than `ttl` ago.

    The Watch runtime is long-lived on AgentCore and every sweep mints fresh ids, so
    nothing is ever overwritten and the caches grew for the life of the process (A11).
    """
    return [cid for cid, ts in stamps.items() if now - ts > ttl]
