"""Eval scoring helpers (pure) and the SLO cost roll-up logic."""

import sys

sys.path.insert(0, "evals")
from scoring import (  # noqa: E402
    check_thresholds,
    evidence_traceability,
    keyword_hits,
    rubric_average,
)


def test_evidence_traceability_counts_real_sources():
    ev = [
        {"source": "ais.find_ais_gaps"},
        {"source": "registry.lookup_vessel"},
        {"source": "my intuition"},
    ]
    assert evidence_traceability(ev) == 0.667
    assert evidence_traceability([]) == 0.0


def test_keyword_hits_is_case_insensitive_and_defaults_to_one():
    assert (
        keyword_hits("Halcyon Marine, flag PW", ["halcyon", "pw", "missing"]) == 0.667
    )
    assert keyword_hits("anything", []) == 1.0


def test_thresholds_are_floors_and_ignore_unmeasured():
    misses = check_thresholds(
        {"recall": 0.7, "precision": 0.9, "rubric_avg": None},
        {"recall": 0.8, "precision": 0.6, "rubric_avg": 3.5},
    )
    assert misses == ["recall=0.7 < 0.8"]


def test_rubric_average():
    assert rubric_average({"bluf": 4, "traceability": 5, "comment": "ok"}) == 4.5
    assert rubric_average(None) is None
