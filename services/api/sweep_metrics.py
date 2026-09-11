"""Alerts raised and duplicates suppressed, as CloudWatch metrics (namespace Argus/Sweeps).

A sweep that raises nothing is normal on a quiet watch. A day of sweeps that raise
nothing means the detectors, the tool plane or the feed are broken in a way that still
returns success, and nothing else notices: the jobs complete, the alarms stay green and
the queue is simply empty. `AlertsRaised` summed over six hours is what an alarm can
watch (gap audit I12).

`DuplicatesSuppressed` is the companion signal: a sudden climb means the sweep is
re-finding windows it has already raised, which is what filled the review queue with
131 alerts in a day before the unique index (P5). Pure payload builder plus a
fire-and-forget publisher, as in review_metrics.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("api")
NAMESPACE = "Argus/Sweeps"


def metric_payload(raised: int, duplicate: int) -> list[dict]:
    """PutMetricData entries for one raise attempt. Zero-valued entries are published on
    purpose: the alarm on this metric treats missing data as breaching, so the API must
    keep reporting even when nothing is being raised."""
    return [
        {
            "MetricName": "AlertsRaised",
            "Dimensions": [],
            "Value": float(max(0, raised)),
            "Unit": "Count",
        },
        {
            "MetricName": "DuplicatesSuppressed",
            "Dimensions": [],
            "Value": float(max(0, duplicate)),
            "Unit": "Count",
        },
    ]


def publish(raised: int = 0, duplicate: int = 0) -> bool:
    """Publish one raise outcome; never raises (a metric must not fail an alert)."""
    if os.getenv("SWEEP_METRICS", "aws").lower() == "off":
        return False
    try:
        import boto3

        boto3.client(
            "cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).put_metric_data(
            Namespace=NAMESPACE, MetricData=metric_payload(raised, duplicate)
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("sweep metric not published: %s", e)
        return False
