"""State transitions on the API are one-way and guarded in SQL (P4, P6).

Job delivery is at least once, a slow agent branch can return long after the case
moved on, and two officers can have the same alert open. Every transition that
matters is therefore expressed as a conditional UPDATE and a 409 when it matches
no row, rather than a read followed by a write that another caller can interleave
with. Before this, `/complete` overwrote an already reviewed report, `/fail` marked
a finished case failed, and a second review flipped a colleague's decision.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services/api/main.py").read_text()


def handler(name: str) -> str:
    """The source of one route handler, up to the next decorator."""
    body = API.split(f"\ndef {name}(", 1)[1]
    return body.split("\n@app.")[0]


def test_complete_only_moves_a_running_investigation():
    src = handler("complete")
    assert "status='running'" in src
    assert "RETURNING mmsi" in src
    assert "HTTPException(409" in src


def test_complete_closes_alerts_by_the_row_mmsi_not_the_report():
    """A report naming another vessel must not close that vessel's alerts."""
    src = handler("complete")
    close = re.search(r"UPDATE alerts SET status='investigated'.*?\)\n", src, re.S)
    assert close, "the alert-closing update moved"
    assert 'done[0]["mmsi"]' in close.group(0)
    assert 'body.report.get("mmsi")' not in close.group(0)


def test_fail_only_moves_a_running_investigation():
    src = handler("fail")
    assert "status='running'" in src
    assert "HTTPException(409" in src


def test_reviewing_an_alert_twice_is_refused():
    src = handler("review_alert")
    assert "review_state='draft'" in src
    assert "409" in src and "404" in src


def test_reviewing_an_investigation_twice_is_refused():
    src = handler("review_investigation")
    assert "review_state='draft'" in src
    assert "status='complete'" in src
    assert "409" in src


def test_the_review_metric_receives_the_reports_priority():
    """The RETURNING list must carry `report`, or the priority label is always null."""
    src = handler("review_investigation")
    returning = re.search(r"RETURNING ([^\"]+)", src).group(1)
    assert "report" in returning
    assert 'get("priority")' in src


def test_the_review_response_does_not_leak_the_report_or_trace():
    src = handler("review_investigation")
    assert 'not in ("trace_id", "report")' in src


def test_every_guarded_update_uses_returning_rather_than_a_read_then_write():
    """A select-then-update would let two callers both see 'running'."""
    for name in ("complete", "fail", "review_alert", "review_investigation"):
        src = handler(name)
        assert "RETURNING" in src, name
