"""Alerts do not duplicate, do not accumulate forever, and lists are bounded (P5).

One day of live sweeps left 131 alerts awaiting review. The sweep consulted only `open`
alerts before raising, so a window already investigated or rejected came back on the
next pass; nothing ever closed a draft nobody had looked at; and the list routes
returned every row. A review queue an officer cannot finish is the same as no queue.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "data/sql/010_alert_dedup_and_expiry.sql").read_text()
API = (ROOT / "services/api/main.py").read_text()
WORKER = (ROOT / "services/api/worker.py").read_text()
AIS_TOOL = (ROOT / "mcp-servers/servers/ais.py").read_text()


def test_the_same_window_can_only_be_raised_once():
    assert "CREATE UNIQUE INDEX IF NOT EXISTS alerts_open_window_uniq" in MIGRATION
    assert "ON alerts (mmsi, kind, started_at)" in MIGRATION
    # Partial: a dismissed alert must not block a genuine later re-occurrence.
    assert "WHERE status <> 'dismissed'" in MIGRATION


def test_the_database_decides_the_duplicate_not_a_read_then_write():
    """Two sweeps can race, so a select-then-insert would still double-raise."""
    handler = API.split("def create_alert(", 1)[1].split("\n@app.", 1)[0]
    assert "ON CONFLICT (mmsi, kind, started_at)" in handler
    assert "DO NOTHING" in handler


def test_a_duplicate_returns_the_alert_that_already_exists():
    handler = API.split("def create_alert(", 1)[1].split("\n@app.", 1)[0]
    assert "if not rows:" in handler
    assert "return existing[0]" in handler


def test_raising_and_suppressing_are_both_measurable():
    handler = API.split("def create_alert(", 1)[1].split("\n@app.", 1)[0]
    assert "sweep_metrics.publish(raised=1, duplicate=0)" in handler
    assert "sweep_metrics.publish(raised=0, duplicate=1)" in handler


def test_the_sweep_metric_reports_zero_rather_than_going_quiet():
    """The alarm treats missing data as breaching, so silence must mean broken."""
    import sys

    sys.path.insert(0, "services/api")
    from sweep_metrics import metric_payload

    names = {m["MetricName"] for m in metric_payload(0, 0)}
    assert names == {"AlertsRaised", "DuplicatesSuppressed"}
    assert all(isinstance(m["Value"], float) for m in metric_payload(0, 0))
    assert metric_payload(1, 0)[0]["Value"] == 1.0


def test_stale_drafts_leave_the_queue_and_say_why():
    assert "CREATE OR REPLACE FUNCTION expire_stale_alerts" in MIGRATION
    assert "status = 'expired'" in MIGRATION
    assert "expired_reason" in MIGRATION
    # Expiry is not deletion: the alert stays readable and the change is audited.
    assert "DELETE FROM alerts" not in MIGRATION
    assert "INSERT INTO audit_events" in MIGRATION
    assert "'alert.expired'" in MIGRATION


def test_only_unreviewed_open_drafts_expire():
    body = MIGRATION.split("CREATE OR REPLACE FUNCTION expire_stale_alerts", 1)[1]
    assert "status = 'open'" in body
    assert "review_state = 'draft'" in body


def test_expiry_runs_on_every_sweep_and_cannot_fail_one():
    assert "def expire_stale_alerts()" in WORKER
    assert "expire_stale_alerts()" in WORKER.split("def run_sweep(", 1)[1]
    helper = WORKER.split("def expire_stale_alerts()", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception" in helper, "a metric or a cleanup must not fail a sweep"
    assert "ALERT_EXPIRY_HOURS" in helper


def test_expiry_can_be_switched_off():
    helper = WORKER.split("def expire_stale_alerts()", 1)[1].split("\ndef ", 1)[0]
    assert "if ALERT_EXPIRY_HOURS <= 0:" in helper


def test_the_sweep_dedups_against_every_alert_it_already_raised():
    """Not just the open ones: an investigated window has still been raised."""
    tool = AIS_TOOL.split("def list_open_alerts(", 1)[1].split("\n@mcp.tool", 1)[0]
    assert "status <> 'dismissed'" in tool
    assert "status='open'" not in tool
    # Bounded, so a case from months ago cannot suppress a real re-occurrence.
    assert "make_interval" in tool and "hours" in tool


def test_list_routes_are_bounded():
    assert "LIST_LIMIT_DEFAULT" in API and "LIST_LIMIT_MAX" in API
    handler = API.split("def alerts(", 1)[1].split("\n@app.", 1)[0]
    assert "LIMIT %s OFFSET %s" in handler
    assert "list_limit(limit)" in handler


def test_the_limit_is_clamped_both_ways():
    ns: dict = {}
    src = API.split("LIST_LIMIT_DEFAULT", 1)[1].split("@app.get", 1)[0]
    exec("LIST_LIMIT_DEFAULT" + src, ns)  # noqa: S102
    f = ns["list_limit"]
    assert f(None) == ns["LIST_LIMIT_DEFAULT"]
    assert f(0) == 1
    assert f(-5) == 1
    assert f(10**9) == ns["LIST_LIMIT_MAX"]
    assert f(50) == 50


def test_the_migration_is_idempotent_like_every_other():
    """`data/sql/*.sql` is re-applied on every start, locally and on Aurora."""
    indexes = re.findall(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?", MIGRATION)
    assert indexes, "no index created; did the migration move?"
    assert all("IF NOT EXISTS" in stmt for stmt in indexes)
    assert MIGRATION.count("CREATE OR REPLACE FUNCTION") >= 1
    assert "CREATE FUNCTION " not in MIGRATION


def test_the_unique_index_can_be_created_on_a_database_that_already_has_duplicates():
    """The migration is re-applied on every ingest start, including on live data.

    Creating this index on the deployed database failed with a UniqueViolation and
    crash-looped the ingest task, because the duplicates the index forbids are exactly
    what a running deployment already had. The existing ones must be folded first, in
    the same file, before the index is created.
    """
    fold = MIGRATION.index("UPDATE alerts a")
    index = MIGRATION.index("CREATE UNIQUE INDEX")
    assert fold < index, (
        "existing duplicates must be folded before the index is created"
    )


def test_folding_dismisses_rather_than_deletes():
    """`investigations.alert_id` references these rows; deleting one orphans a case."""
    fold = MIGRATION[
        MIGRATION.index("UPDATE alerts a") : MIGRATION.index("CREATE UNIQUE INDEX")
    ]
    assert "status = 'dismissed'" in fold
    assert "dismissed_reason" in fold
    assert "DELETE" not in fold.upper()


def test_folding_keeps_the_oldest_of_each_group():
    """The oldest is the one officers have been looking at and investigations reference."""
    fold = MIGRATION[
        MIGRATION.index("UPDATE alerts a") : MIGRATION.index("CREATE UNIQUE INDEX")
    ]
    assert "(b.created_at, b.id) < (a.created_at, a.id)" in fold


def test_the_fold_qualifies_its_columns():
    """`UPDATE alerts a` with a correlated subquery over the same table needs it."""
    fold = MIGRATION[
        MIGRATION.index("UPDATE alerts a") : MIGRATION.index("CREATE UNIQUE INDEX")
    ]
    assert "a.status <> 'dismissed'" in fold
    assert "coalesce(a.details" in fold
