"""The ingest task reports on itself, so a silent feed can page (I4).

The task reconnects forever. An expired AISStream key, a subscription matching nothing,
or a silent upstream all leave it RUNNING with a healthy container while no position is
written, and nothing in ECS notices. The API's own staleness view needs the API and the
collector to be up, which is exactly what a platform outage removes.
"""

import sys
from pathlib import Path

sys.path.insert(0, "services/ais-replay")

ROOT = Path(__file__).resolve().parents[1]
REPLAY = (ROOT / "services/ais-replay/replay.py").read_text()


def test_the_payload_carries_age_and_both_counts():
    from feed_metrics import metric_payload

    names = {m["MetricName"] for m in metric_payload(12.0, 5, 1)}
    assert names == {"LastPositionAge", "PositionsStored", "PositionsDropped"}
    age = next(
        m for m in metric_payload(12.0, 5, 1) if m["MetricName"] == "LastPositionAge"
    )
    assert age["Value"] == 12.0 and age["Unit"] == "Seconds"


def test_the_mode_is_a_dimension_so_replay_and_live_are_distinguishable():
    from feed_metrics import metric_payload

    for m in metric_payload(1.0, 1, 0, "replay"):
        assert {"Name": "mode", "Value": "replay"} in m["Dimensions"]


def test_negative_values_never_reach_cloudwatch():
    from feed_metrics import metric_payload

    assert all(m["Value"] >= 0 for m in metric_payload(-5, -1, -1))


def test_health_reports_age_and_resets_its_counters():
    from feed_metrics import FeedHealth

    h = FeedHealth()
    h.stored_one()
    h.stored_one()
    h.dropped_one()
    age, stored, dropped = h.take()
    assert (stored, dropped) == (2, 1)
    assert age >= 0
    # The counts are per interval, not totals, so a rate reads directly off the metric.
    assert h.take()[1:] == (0, 0)


def test_a_stored_position_resets_the_age():
    import time

    from feed_metrics import FeedHealth

    h = FeedHealth()
    h.last_position_at = time.time() - 600
    assert h.take()[0] > 500
    h.stored_one()
    assert h.take()[0] < 5


def test_the_heartbeat_runs_whether_or_not_the_socket_is_connected():
    """A heartbeat published only inside the receive loop stops exactly when the feed
    does, which is the moment it is needed."""
    assert "async def heartbeat(" in REPLAY
    beat = REPLAY.split("async def heartbeat(", 1)[1].split("\nasync def ", 1)[0]
    assert "while True:" in beat
    assert "await asyncio.sleep(HEARTBEAT_S)" in beat
    # It is its own task, started outside the reconnect loop.
    assert REPLAY.count("asyncio.create_task(heartbeat(") == 2


def test_both_ingest_modes_publish_the_same_heartbeat():
    live = REPLAY.split("async def live_aisstream(", 1)[1]
    replay = REPLAY.split("async def replay_stream(", 1)[1].split("\nasync def ", 1)[0]
    assert 'heartbeat(health, "live")' in live
    assert 'heartbeat(health, "replay")' in replay


def test_the_module_is_in_the_image():
    assert "feed_metrics.py" in (ROOT / "services/ais-replay/Dockerfile").read_text()


def test_publishing_never_takes_the_feed_down():
    src = (ROOT / "services/ais-replay/feed_metrics.py").read_text()
    publish = src.split("def publish(", 1)[1]
    assert "except Exception" in publish
    assert "return False" in publish
