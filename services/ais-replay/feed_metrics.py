"""The ingest task's own heartbeat as a CloudWatch metric (I4).

The task reconnects forever. An expired AISStream key, a subscription that matches
nothing, or a silent upstream all leave it RUNNING with a healthy container while no
position is written, so nothing in ECS or the load balancer notices. The API's own
staleness view needs the API and the collector to be up, which is exactly what a
platform-wide outage takes away.

So the task reports on itself: seconds since the last stored position, and counts of
what it stored and dropped. An alarm on `LastPositionAge` with missing data treated as
breaching then fires both when the feed goes quiet and when this task stops publishing
at all. Pure payload builder plus a fire-and-forget publisher, as in review_metrics.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("replay")
NAMESPACE = "Argus/Feed"


def metric_payload(
    age_s: float, stored: int, dropped: int, mode: str = "live"
) -> list[dict]:
    """PutMetricData entries for one heartbeat.

    `age_s` is seconds since the last position this task stored. `stored` and `dropped`
    are counts for the interval just ended, not totals, so a rate is readable directly.
    """
    dims = [{"Name": "mode", "Value": mode}]
    return [
        {
            "MetricName": "LastPositionAge",
            "Dimensions": dims,
            "Value": max(0.0, float(age_s)),
            "Unit": "Seconds",
        },
        {
            "MetricName": "PositionsStored",
            "Dimensions": dims,
            "Value": float(max(0, stored)),
            "Unit": "Count",
        },
        {
            "MetricName": "PositionsDropped",
            "Dimensions": dims,
            "Value": float(max(0, dropped)),
            "Unit": "Count",
        },
    ]


def publish(age_s: float, stored: int, dropped: int, mode: str = "live") -> bool:
    """Publish one heartbeat; never raises (a metric must not stop the feed)."""
    if os.getenv("FEED_METRICS", "aws").lower() == "off":
        return False
    try:
        import boto3

        boto3.client(
            "cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).put_metric_data(
            Namespace=NAMESPACE, MetricData=metric_payload(age_s, stored, dropped, mode)
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("feed heartbeat not published: %s", e)
        return False


class FeedHealth:
    """What the ingest task knows about itself between heartbeats."""

    def __init__(self) -> None:
        self.last_position_at = time.time()
        self.stored = 0
        self.dropped = 0

    def stored_one(self) -> None:
        self.last_position_at = time.time()
        self.stored += 1

    def dropped_one(self) -> None:
        self.dropped += 1

    def take(self) -> tuple[float, int, int]:
        """Age since the last stored position, and the counts since the last call."""
        stored, dropped = self.stored, self.dropped
        self.stored = self.dropped = 0
        return time.time() - self.last_position_at, stored, dropped
