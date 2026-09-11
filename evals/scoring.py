"""Pure scoring helpers for the node evals (no network), unit-tested."""

from __future__ import annotations

from datetime import datetime

TOOL_PREFIXES = (
    "ais.",
    "registry.",
    "geo.",
    "imagery.",
    "positions",
    "registry",
    "ownership_network",
    "local-registry",
    "local-list",
    "opensanctions",
    "aisstream",
    "vessels",
)


def evidence_traceability(evidence: list[dict]) -> float:
    """Share of evidence entries whose source names a real tool or data source."""
    if not evidence:
        return 0.0
    ok = sum(
        1
        for e in evidence
        if any(str(e.get("source", "")).startswith(p) for p in TOOL_PREFIXES)
    )
    return round(ok / len(evidence), 3)


def keyword_hits(text: str, keywords: list) -> float:
    """Share of expected keywords present (case-insensitive); 1.0 when nothing is expected.
    An entry may be a list of alternatives ("PW" or "Palau"): any one of them counts."""
    if not keywords:
        return 1.0
    low = text.lower()

    def hit(k) -> bool:
        alts = k if isinstance(k, list) else [k]
        return any(str(a).lower() in low for a in alts)

    return round(sum(1 for k in keywords if hit(k)) / len(keywords), 3)


def check_thresholds(scores: dict, thresholds: dict) -> list[str]:
    """Thresholds are floors. Unmeasured scores (None) never fail the gate; they are reported."""
    misses = []
    for k, floor in thresholds.items():
        v = scores.get(k)
        if v is None:
            continue
        if v < floor:
            misses.append(f"{k}={v} < {floor}")
    return misses


def rubric_average(judge: dict | None) -> float | None:
    if not judge:
        return None
    vals = [
        v
        for k, v in judge.items()
        if k != "samples" and isinstance(v, int | float) and not isinstance(v, bool)
    ]
    return round(sum(vals) / len(vals), 2) if vals else None


def overlap(a0, a1, b0, b1) -> bool:
    def f(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    return f(a0) <= f(b1) and f(b0) <= f(a1)


def raised_since(alerts: list[dict], since: str | None) -> list[dict]:
    """Only the alerts raised at or after `since` (an ISO timestamp), or all of them.

    The gate used to score every alert the deployment had ever raised, so a stack that
    had been running for a week was judged on a week of accumulated alerts rather than
    on the sweep just run. Recall then drifted up as old alerts accumulated and the
    gate stopped measuring the change under test (gap audit P12).
    """
    if not since:
        return list(alerts)
    return [a for a in alerts if str(a.get("created_at") or "") >= since]


def score(truth: list[dict], alerts: list[dict]) -> dict:
    """Recall and precision of a sweep against the scenario's labelled anomalies.

    Precision counts each matching alert once, not each matched truth item: raising the
    same anomaly five times used to score exactly as well as raising it once, because
    the matched set was keyed on the alert index but divided into the count of scored
    alerts only through distinct matches. A duplicate is a false alarm from the
    officer's side of the desk, so it costs precision here too (gap audit P12).
    """
    matched, hits = set(), []
    # One alert per truth item is a hit; the rest are duplicates.
    for t in truth:
        found: list[int] = []
        for i, a in enumerate(alerts):
            parties = {t["mmsi"], (t.get("details") or {}).get("with_mmsi")}
            if (
                a["mmsi"] in parties
                and a["kind"] == t["kind"]
                and a.get("started_at")
                and a.get("ended_at")
                and overlap(
                    t["started_at"], t["ended_at"], a["started_at"], a["ended_at"]
                )
            ):
                found.append(i)
        if found:
            matched.add(found[0])
        hits.append(
            {
                "mmsi": t["mmsi"],
                "kind": t["kind"],
                "detected": bool(found),
                "duplicates": max(0, len(found) - 1),
            }
        )
    recall = sum(h["detected"] for h in hits) / max(len(truth), 1)
    # Precision counts false alarms among the kinds the scenario labels. An alert of a kind
    # the ground truth does not enumerate (a zone incursion in a scenario that only labels
    # gaps and rendezvous) is a finding the scorer cannot judge, not a false positive.
    scored_kinds = {t["kind"] for t in truth}
    scored = [i for i, a in enumerate(alerts) if a["kind"] in scored_kinds]
    precision = len(matched) / max(len(scored), 1)
    duplicates = sum(h["duplicates"] for h in hits)
    by_kind = {
        k: sum(h["detected"] for h in hits if h["kind"] == k)
        / max(sum(1 for h in hits if h["kind"] == k), 1)
        for k in {h["kind"] for h in hits}
    }
    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "recall_by_kind": by_kind,
        "alerts": len(alerts),
        "duplicates": duplicates,
        "unscored": len(alerts) - len(scored),
        "truth": len(truth),
        "details": hits,
    }


def judge_consensus(votes: list[dict]) -> dict:
    """Average the numeric rubric dimensions over several judge samples; keep the first
    comment. Non-numeric or missing dimensions are ignored."""
    dims: dict[str, list[float]] = {}
    comment = ""
    for v in votes:
        for k, x in (v or {}).items():
            if isinstance(x, int | float) and not isinstance(x, bool):
                dims.setdefault(k, []).append(float(x))
            elif k == "comment" and not comment and isinstance(x, str):
                comment = x
    out: dict = {k: round(sum(xs) / len(xs), 2) for k, xs in dims.items()}
    out["comment"] = comment
    out["samples"] = len(votes)
    return out
