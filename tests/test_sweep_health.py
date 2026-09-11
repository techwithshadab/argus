"""A broken sweep says so, and a long-lived runtime does not grow forever (A4, A11, A15).

A denied Cedar policy, a SQL error or a gateway refusal came back as an error result
that nothing read, so the detector's output became an empty dict and the sweep reported
zero candidates exactly as a quiet watch does. The sweep request parser compounded it:
a trailing full stop on the `until` timestamp made every detector raise, and the missing
error check then hid all five failures behind a clean-looking summary.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, "agents")

ROOT = Path(__file__).resolve().parents[1]
WATCH = (ROOT / "agents/watch/app.py").read_text()


def parser():
    """`parse_sweep_request` alone: the module imports strands, which unit tests may not."""
    start = WATCH.index("def parse_sweep_request")
    end = WATCH.index("\n\n\n", start)
    ns: dict = {"re": re}
    exec(compile(WATCH[start:end], "watch-parser", "exec"), ns)  # noqa: S102
    return ns["parse_sweep_request"]


def test_one_broken_detector_is_not_a_broken_sweep():
    from shared.sweep import sweep_failure

    assert sweep_failure([]) is None
    assert sweep_failure(["gaps"]) is None
    assert sweep_failure(["gaps", "conflicts", "loitering", "rendezvous"]) is None


def test_every_detector_failing_fails_the_sweep():
    from shared.sweep import DETECTOR_KEYS, sweep_failure

    reason = sweep_failure(list(DETECTOR_KEYS))
    assert reason and "every detector failed" in reason


def test_a_failed_dedup_lookup_alone_does_not_fail_the_sweep():
    """`open_alerts` is a sixth call; losing it disables dedup, it does not blind the sweep."""
    from shared.sweep import DETECTOR_KEYS, sweep_failure

    assert "open_alerts" not in DETECTOR_KEYS
    assert sweep_failure(["open_alerts"]) is None


def test_the_detector_loop_reads_the_error_flag():
    loop = WATCH.split("def _detect(", 1)[1].split("\n@tool", 1)[0]
    assert 'res.get("status") == "error"' in loop
    assert 'res.get("isError")' in loop
    assert "_failed" in loop


def test_the_sweep_fails_loudly_and_reports_partial_failures():
    body = WATCH.split("def sweep(self,", 1)[1].split("\ndef ", 1)[0]
    assert "sweep_failure(failed)" in body
    assert "raise RuntimeError(broken)" in body
    assert '"failed_detectors": failed' in body


def test_candidate_caches_are_pruned_by_time():
    from shared.sweep import CANDIDATE_TTL_S, expired_candidates

    now = 10_000.0
    stamps = {"fresh": now - 10, "old": now - CANDIDATE_TTL_S - 1}
    assert expired_candidates(stamps, now) == ["old"]
    assert expired_candidates({}, now) == []


def test_all_three_caches_are_pruned_together():
    """Dropping a candidate but keeping its disposition would double-count it."""
    body = WATCH.split("def sweep(self,", 1)[1].split("\ndef ", 1)[0]
    prune = body.split("expired_candidates(", 1)[1].split("for c in cands", 1)[0]
    for cache in ("_CANDIDATE_TS", "_CANDIDATES", "_DISPOSITIONS"):
        assert cache in prune, cache


def test_pruning_happens_before_this_sweeps_candidates_are_stored():
    body = WATCH.split("def sweep(self,", 1)[1].split("\ndef ", 1)[0]
    assert body.index("expired_candidates(") < body.index('_CANDIDATES[c["id"]]')


def test_a_trailing_full_stop_no_longer_reaches_the_detectors():
    f = parser()
    assert f("Sweep the last 6 h until 2026-09-01T08:00:00Z.") == (
        6.0,
        "2026-09-01T08:00:00Z",
    )
    assert f("Sweep the last 6 h until 2026-09-01T08:00:00+00:00, please") == (
        6.0,
        "2026-09-01T08:00:00+00:00",
    )


def test_the_clean_forms_still_parse():
    f = parser()
    assert f("Sweep the last 1.5 h until 2026-09-01T08:00:00Z") == (
        1.5,
        "2026-09-01T08:00:00Z",
    )
    assert f("please sweep") == (12.0, None)


def test_every_parsed_until_is_a_timestamp_the_detectors_accept():
    from datetime import datetime

    f = parser()
    for text in (
        "last 6 h until 2026-09-01T08:00:00Z.",
        "last 6 h until 2026-09-01T08:00:00+00:00,",
        'last 6 h until 2026-09-01T08:00:00Z"',
    ):
        _, until = f(text)
        # The exact call the detectors make; it used to raise on all three.
        datetime.fromisoformat(until.replace("Z", "+00:00"))
