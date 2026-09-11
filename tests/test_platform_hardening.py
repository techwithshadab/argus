"""Inputs are bounded, state changes are atomic, and nothing leaks (P7-P19).

These are the ways the platform could be pushed into a bad state by a caller acting
normally: an alert id for another vessel, two callers racing to open the same case, an
unbounded window or a held button, a model's free text reaching a metric label, the
answer key served to anyone, and evidence that was never captured behind a report that
reads complete.
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "services/api/main.py").read_text()
OFFICER = (ROOT / "services/api/officerauth.py").read_text()
REPLAY = (ROOT / "services/ais-replay/replay.py").read_text()

sys.path.insert(0, "services/ais-replay")


def handler(name: str) -> str:
    return API.split(f"\ndef {name}(", 1)[1].split("\n@app.", 1)[0]


def compiled(marker: str, end: str, extra=None):
    """A pure block of main.py, compiled alone: the module imports psycopg."""
    start = API.index(marker)
    ns: dict = dict(extra or {})
    exec(compile(API[start : API.index(end, start)], "api-fragment", "exec"), ns)  # noqa: S102
    return ns


# ---- P7 ----
def test_an_unknown_alert_is_a_404_not_a_500():
    src = handler("start_investigation")
    assert 'raise HTTPException(404, "no alert with that id")' in src


def test_an_alert_for_another_vessel_is_refused():
    src = handler("start_investigation")
    assert 'int(alert["mmsi"]) != mmsi' in src
    assert "HTTPException(\n                400," in src or "HTTPException(400" in src


def test_a_malformed_alert_id_never_reaches_the_database():
    assert "_alert_id" in API
    assert "must be a UUID" in API


def test_the_trigger_cannot_be_free_text():
    """It reaches the job's idempotency key, so any string defeated the dedup index."""
    assert "TRIGGERS = " in API
    model = API.split("class InvestigationIn(", 1)[1].split("\nclass ", 1)[0]
    assert "trigger must be one of" in model


def test_the_auto_investigate_path_still_has_a_valid_trigger():
    """`create_alert` opens an investigation with the alert's kind as the trigger."""
    ns = compiled("ALERT_KINDS = (", "_KIND_ALIASES = {")
    triggers = compiled(
        "TRIGGERS = (", "class InvestigationIn", {"ALERT_KINDS": ns["ALERT_KINDS"]}
    )
    for kind in ns["ALERT_KINDS"]:
        assert kind in triggers["TRIGGERS"], kind
    assert "manual" in triggers["TRIGGERS"] and "policy" in triggers["TRIGGERS"]


# ---- P8 ----
def test_opening_an_investigation_is_one_transaction():
    src = API.split("def open_investigation(", 1)[1].split("\ndef ", 1)[0]
    assert "with pool.connection() as conn" in src
    assert "conn.rollback()" in src
    assert "conn.commit()" in src


def test_the_loser_of_the_race_leaves_no_orphan():
    src = API.split("def open_investigation(", 1)[1].split("\ndef ", 1)[0]
    rollback = src.split("if row is None:", 1)[1].split("job_id = row", 1)[0]
    assert "conn.rollback()" in rollback
    assert '"deduplicated": True' in rollback


def test_the_queue_send_happens_after_the_commit():
    """A worker must never claim a job whose investigation row does not exist yet."""
    src = API.split("def open_investigation(", 1)[1].split("\ndef ", 1)[0]
    assert src.index("conn.commit()") < src.index('queues["investigation"].send')


# ---- P9 ----
def test_the_track_window_is_clamped():
    assert "TRACK_MAX_HOURS = 168" in API
    src = handler("track")
    assert "min(float(hours), TRACK_MAX_HOURS)" in src
    assert "LIMIT %s" in src


def test_manual_sweeps_share_a_key_within_a_minute():
    src = handler("sweep")
    assert "MANUAL_SWEEP_BUCKET_MIN" in src
    assert "sweep_key(datetime.now(UTC), 0, hours)" not in src


def test_the_sweep_key_helper_itself_is_unchanged():
    """tests/test_jobs.py pins interval 0 as a new key every second; the caller buckets."""
    jobs = (ROOT / "services/api/jobqueue.py").read_text()
    assert "def sweep_key(" in jobs


def test_progress_is_capped():
    assert "PROGRESS_MAX_ENTRIES" in API
    src = handler("investigation_progress")
    assert "jsonb_array_elements" in src and "LIMIT %s" in src


# ---- P10 ----
def test_the_metric_label_helper_escapes_what_prometheus_reserves():
    label = compiled("def label(value)", '@app.get("/metrics")')["label"]
    assert label('a"b') == 'a\\"b'
    assert label("a\nb") == "a\\nb"
    assert label("a\\b") == "a\\\\b"
    assert label(None) == ""
    assert label("high") == "high"


def test_every_metric_label_goes_through_it():
    metrics = API.split('@app.get("/metrics")', 1)[1]
    for line in re.findall(r"f\'argus_[a-z_]+\{\{[^\n]*", metrics):
        for field in re.findall(r'="\{([^}]+)\}"', line):
            assert field.startswith("label("), line


def test_an_unknown_alert_kind_or_severity_is_refused():
    model = API.split("class AlertIn(", 1)[1].split("\nclass ", 1)[0]
    assert "kind must be one of" in model
    assert "severity must be one of" in model
    assert "Field(ge=0.0, le=1.0)" in model


def test_the_alias_map_still_runs_before_the_check():
    """Models echo detector names; rejecting `mmsi_conflict` outright would lose alerts."""
    model = API.split("class AlertIn(", 1)[1].split("\nclass ", 1)[0]
    kind = model.split("def _kind(", 1)[1].split("@field_validator", 1)[0]
    assert kind.index("_KIND_ALIASES.get") < kind.index("not in ALERT_KINDS")


def test_a_malformed_timestamp_is_refused_rather_than_500ing_an_agent_route():
    model = API.split("class AlertIn(", 1)[1].split("\nclass ", 1)[0]
    assert "must be an ISO 8601 timestamp" in model


# ---- P11 ----
def test_the_answer_key_needs_the_operator_role():
    src = handler("ground_truth")
    assert "require_operator(request)" in src
    assert "Depends(current_officer)" in src


# ---- P13 ----
def test_live_mode_does_not_load_the_scenario_onto_real_vessels():
    main = REPLAY.split("def main()", 1)[1]
    live = main.index('if MODE == "live"')
    assert main.index("load_reference(gen)") > live, (
        "reference data must load after the branch"
    )


def test_live_mode_publishes_an_empty_answer_key():
    """A box switched from replay to live would otherwise keep serving the old one."""
    main = REPLAY.split("def main()", 1)[1]
    live_branch = main.split('if MODE == "live"', 1)[1].split("return", 1)[0]
    assert '"ground_truth": []' in live_branch


# ---- P14 ----
PUBLIC_ALB = "argus--publi-kh4lc5wmytck-2034727193.us-east-1.elb.amazonaws.com"
INTERNAL_ALB = (
    "internal-argus--inter-hqhndh5o4uya-2088518419.us-east-1.elb.amazonaws.com"
)


def public_balancer(headers, host=PUBLIC_ALB):
    """The predicate under test, with PUBLIC_HOST set the way the platform stack sets it."""
    sys.path.insert(0, "services/api")
    import officerauth

    old = os.environ.get("PUBLIC_HOST")
    os.environ["PUBLIC_HOST"] = host
    try:
        return officerauth.arrived_through_the_public_balancer(headers)
    finally:
        if old is None:
            os.environ.pop("PUBLIC_HOST", None)
        else:
            os.environ["PUBLIC_HOST"] = old


def test_metrics_and_health_are_not_reachable_through_the_public_balancer():
    assert "INTERNAL_ONLY_ROUTES" in OFFICER
    assert public_balancer({"host": PUBLIC_ALB})
    assert public_balancer({"host": PUBLIC_ALB + ":443"})


def test_the_ui_proxy_marks_public_requests_that_would_otherwise_look_internal():
    """The hole this reopened once: the UI's nginx proxies /api/* to the API and
    rewrites Host to `api:8000`, so a public `/api/metrics` with any bearer header
    reached the API looking like an internal call and served the whole exposition.
    nginx marks them; the API refuses anything carrying the mark."""
    assert public_balancer({"host": "api:8000", "x-argus-public": "1"})
    conf = (ROOT / "services/ui/nginx.conf").read_text()
    assert 'proxy_set_header X-Argus-Public "1"' in conf


def test_the_sse_read_deadline_outlives_its_blocking_read():
    """The read deadline must stay above the block interval so it never interrupts a
    legitimate blocking read. (The errors seen in production came from the server
    closing idle connections mid-block, which raises regardless of this value; the
    retry in the generator is what keeps the stream alive.)"""
    block_ms = int(re.search(r"SSE_BLOCK_MS = (\d+)", API).group(1))
    timeout_s = int(re.search(r"SSE_TIMEOUT_S = (\d+)", API).group(1))
    assert timeout_s > block_ms / 1000
    assert "socket_timeout=SSE_TIMEOUT_S" in API


def test_a_dropped_sse_read_does_not_end_the_stream():
    """A closed stream leaves the progress panel frozen, so a read error pings and
    retries instead of falling out of the generator."""
    for handler in ("event_stream", "stream"):
        body = API.split(f"async def {handler}(", 1)[1].split("\n@app.", 1)[0]
        assert "except redis.RedisError" in body, handler
        assert "continue" in body, handler


def test_the_ui_pins_its_api_upstream_to_ipv4():
    """Service Connect advertises an AAAA (2600:f0f0::2) that the VPC, which has no IPv6
    CIDR, cannot route. nginx caches both records at start and round-robins, so a steady
    ~15% of /api/* calls failed with "Network unreachable" and a restart never helped:
    every new worker cached the same bad address. The entrypoint resolves IPv4 only and
    pins it before nginx starts."""
    script = (ROOT / "services/ui/05-pin-api-ipv4.sh").read_text()
    assert "getent ahostsv4 api" in script
    # Falling back to the bare name makes nginx refuse to start ("host not found in
    # upstream"), which crash-loops the task and trips the deployment circuit breaker.
    assert "127.0.0.1:8000" in script
    dockerfile = (ROOT / "services/ui/Dockerfile").read_text()
    assert "/docker-entrypoint.d/05-pin-api-ipv4.sh" in dockerfile


def test_the_ui_proxy_uses_a_static_service_connect_upstream():
    """`api` is a Service Connect name resolved by the container's own resolver at
    start. A runtime `resolver` pointed at the VPC DNS (169.254.169.253) produced
    "api could not be resolved (Host not found)" and 502s, because VPC DNS does not
    carry Service Connect names. Keep proxy_pass static."""
    conf = (ROOT / "services/ui/nginx.conf").read_text()
    assert "proxy_pass http://api:8000/;" in conf
    directives = [
        ln.strip()
        for ln in conf.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert not any(ln.startswith("resolver ") for ln in directives)


def test_the_collectors_scrape_is_not_mistaken_for_a_public_request():
    """The regression this replaced: the collector scrapes the API *through the internal
    balancer*, so its requests carry x-forwarded-for. Keying the block on that header
    404'd every scrape and the API's metrics stopped reaching Prometheus."""
    scrape = {"host": INTERNAL_ALB + ":8000", "x-forwarded-for": "10.0.9.253"}
    assert not public_balancer(scrape)


def test_the_health_check_reaches_the_task_directly():
    assert not public_balancer({"host": "10.0.11.201:8000"})


def test_locally_there_is_no_public_balancer_so_nothing_is_blocked():
    assert not public_balancer({"host": "localhost:8000"}, host="")


def test_they_stay_public_so_the_health_check_and_the_scrape_still_work():
    assert '("GET", "/health")' in OFFICER.split("PUBLIC_ROUTES = ", 1)[1][:120]
    assert '("GET", "/metrics")' in OFFICER.split("PUBLIC_ROUTES = ", 1)[1][:120]


# ---- P15 ----
def test_positions_are_batched():
    from aisvalidate import BATCH_MAX, BATCH_MAX_S, should_flush

    assert should_flush(BATCH_MAX, 0.0)
    assert should_flush(1, BATCH_MAX_S + 0.1)
    assert not should_flush(0, 999.0), "an empty batch is not worth a round trip"
    assert not should_flush(1, 0.0)


def test_the_batch_window_stays_well_inside_the_heartbeat():
    """A batch that outlived the heartbeat would make a healthy feed look stalled."""
    from aisvalidate import BATCH_MAX_S

    heartbeat = int(re.search(r'FEED_HEARTBEAT_S", "(\d+)"', REPLAY).group(1))
    assert BATCH_MAX_S * 5 < heartbeat


def test_the_row_tuples_match_the_insert():
    from aisvalidate import position_rows

    vessels, positions = position_rows(
        [
            {
                "mmsi": 1,
                "ts": "t",
                "lon": 24.6,
                "lat": 35.1,
                "sog": 1.0,
                "cog": 2.0,
                "nav_status": "0",
                "source": "aisstream",
                "name": "X",
            }
        ]
    )
    assert vessels == [(1, "X")]
    assert positions[0][2] == "SRID=4326;POINT(24.6 35.1)"
    assert len(positions[0]) == 7


def test_position_inserts_never_gained_a_conflict_clause():
    """`positions` has no primary key on purpose: spoofed MMSIs share (mmsi, ts)."""
    writer = REPLAY.split("async def flush_batch(", 1)[1].split("\nasync def ", 1)[0]
    assert "INSERT INTO positions" in writer
    # Only the positions statements; the vessels upsert legitimately has one.
    for stmt in re.findall(r"INSERT INTO positions[^\"]*", writer):
        assert "ON CONFLICT" not in stmt


# ---- P16 ----
def test_cors_is_not_a_wildcard():
    assert 'allow_origins=["*"]' not in API
    assert "CORS_ALLOW_ORIGINS" in API


# ---- P17 ----
def test_both_streams_close_their_redis_client():
    assert API.count("await r.aclose()") == 2
    for route in ('@app.get("/events")', '@app.get("/stream")'):
        body = API.split(route, 1)[1].split("\n@app.", 1)[0]
        assert "finally:" in body, route


# ---- P18 ----
def test_the_evidence_is_captured_before_the_case_is_marked_complete():
    src = handler("complete")
    assert src.index("snapshot_investigation(inv_id") < src.index("status='complete'")


def test_a_failed_snapshot_fails_the_call_rather_than_being_logged():
    src = handler("complete")
    assert "HTTPException(\n            503" in src or "HTTPException(503" in src


def test_snapshotting_twice_does_not_duplicate_the_evidence():
    """Delivery is at least once and the snapshot now runs before the status guard."""
    src = API.split("def snapshot_investigation(", 1)[1].split("\ndef ", 1)[0]
    assert "DELETE FROM evidence_snapshots" in src


# ---- P19 ----
def test_the_audit_docstring_describes_what_the_code_does():
    doc = API.split("def audit(", 1)[1].split('"""', 2)[1]
    assert "Never raises" not in doc
    assert "Raises if the write fails" in doc


def test_recording_a_score_names_the_principal():
    src = handler("record_eval")
    assert "require_operator(request)" in src
    assert 'getattr(request.state, "officer", "")' in src
    assert 'f"iam:{role}"' in src


# ---- agent route allowlist ----
def test_the_orchestrator_may_read_a_vessel_track():
    """`gap_position` derives where the vessel went dark from the track the platform
    already holds, because the Tasking agent has no AIS tool and, handed only prose,
    once proposed a SAR collection over New York for a vessel off Singapore. The route
    was missing from _AGENT_ROUTES, so the call was refused 88 times an hour and every
    tasking recommendation was recorded unverified."""
    sys.path.insert(0, "services/api")
    from callerauth import matches_route

    routes = (("GET", "/vessels/*/track"),)
    assert matches_route(routes, "/vessels/314308000/track", "GET")
    # and no wider than that
    assert not matches_route(routes, "/vessels/1/track/extra", "GET")
    assert not matches_route(routes, "/vessels/1/track", "POST")
    assert '("GET", "/vessels/*/track")' in API


def test_audit_payloads_survive_a_uuid():
    """`jobs.id` arrives from psycopg as a UUID, and `Jsonb` calls `json.dumps`, so an
    audit payload carrying one raised *after* the state change it records:
    `open_investigation` committed its rows and then returned 500 with the job stuck
    `queued`. Every Jsonb goes through the wrapper so no call site can reintroduce it."""
    # the coercions the wrapper has to make
    assert "isinstance(obj, UUID)" in API
    assert "isinstance(obj, datetime | date)" in API
    assert "def jsonb(" in API and "dumps=_dump_json" in API
    # and no raw Jsonb( is left at a call site
    body = API.split("def jsonb(", 1)[1]
    calls = [
        ln
        for ln in body.splitlines()
        if "Jsonb(" in ln
        and "return Jsonb" not in ln
        and not ln.lstrip().startswith("#")
    ]
    assert not calls, calls
