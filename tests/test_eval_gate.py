"""The eval gate measures the change under test, and a score is not an opinion (P12).

Three ways the gate could be passed without earning it: it scored every alert the
deployment had ever raised rather than the sweep just run, so recall drifted upward as
alerts accumulated; a duplicate alert cost nothing, so raising the same anomaly five
times scored exactly as well as raising it once; and `POST /evals` took a passing score
from anyone the API admitted, for a suite that need not have run at all.
"""

import sys
from pathlib import Path

sys.path.insert(0, "evals")

ROOT = Path(__file__).resolve().parents[1]

TRUTH = [
    {
        "mmsi": 232003453,
        "kind": "ais_gap",
        "started_at": "2026-09-01T02:00:00+00:00",
        "ended_at": "2026-09-01T08:00:00+00:00",
    }
]


def alert(**kw):
    base = {
        "mmsi": 232003453,
        "kind": "ais_gap",
        "started_at": "2026-09-01T02:30:00+00:00",
        "ended_at": "2026-09-01T07:30:00+00:00",
        "created_at": "2026-09-01T09:00:00+00:00",
    }
    return {**base, **kw}


def test_one_correct_alert_scores_perfectly():
    from scoring import score

    s = score(TRUTH, [alert()])
    assert s["recall"] == 1.0 and s["precision"] == 1.0
    assert s["duplicates"] == 0


def test_raising_the_same_anomaly_five_times_costs_precision():
    """A duplicate is a false alarm from the officer's side of the desk."""
    from scoring import score

    s = score(TRUTH, [alert() for _ in range(5)])
    assert s["recall"] == 1.0, "the anomaly was still found"
    assert s["precision"] == 0.2, "but four of the five alerts were noise"
    assert s["duplicates"] == 4


def test_a_missed_anomaly_still_scores_zero_recall():
    from scoring import score

    s = score(TRUTH, [])
    assert s["recall"] == 0.0 and s["duplicates"] == 0


def test_only_the_runs_own_alerts_are_scored():
    from scoring import raised_since

    old = alert(created_at="2026-08-25T00:00:00+00:00")
    new = alert(created_at="2026-09-01T09:00:00+00:00")
    kept = raised_since([old, new], "2026-09-01T08:00:00+00:00")
    assert kept == [new]


def test_without_a_window_everything_is_scored():
    from scoring import raised_since

    rows = [alert(), alert(created_at="2026-08-01T00:00:00+00:00")]
    assert raised_since(rows, None) == rows
    assert raised_since(rows, "") == rows


def test_an_alert_with_no_timestamp_is_not_counted_as_recent():
    from scoring import raised_since

    assert raised_since([{"mmsi": 1}], "2026-09-01T00:00:00+00:00") == []


def test_a_week_of_accumulated_alerts_cannot_inflate_the_gate():
    """The regression this closes: the same sweep judged against a week of history."""
    from scoring import raised_since, score

    history = [
        alert(created_at=f"2026-08-2{d}T00:00:00+00:00", mmsi=100000 + d)
        for d in range(1, 8)
    ]
    run = [alert()]
    everything = score(TRUTH, history + run)
    just_this_run = score(
        TRUTH, raised_since(history + run, "2026-09-01T00:00:00+00:00")
    )
    assert just_this_run["alerts"] == 1
    assert everything["alerts"] > just_this_run["alerts"]


def test_recording_a_score_needs_the_operator_role():
    api = (ROOT / "services/api/main.py").read_text()
    assert "def require_operator(" in api
    handler = api.split("def record_eval(", 1)[1].split("\n@app.", 1)[0]
    assert "require_operator(request)" in handler
    # The audit entry names the principal that wrote the score, not a constant.
    assert '"evals",\n        "system"' not in handler
    guard = api.split("def require_operator(", 1)[1].split("\n@app.", 1)[0]
    assert "officer_roles()" in guard and "403" in guard


def test_the_gate_can_run_its_own_sweep():
    runner = (ROOT / "evals/run_eval.py").read_text()
    assert '"--sweep"' in runner
    assert "/sweep?hours=12" in runner
