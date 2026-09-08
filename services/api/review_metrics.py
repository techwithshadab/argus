"""The watch officer's verdict as a CloudWatch metric, next to the AgentCore Evaluations
scores (ADR-0012). Human judgement and model judgement then sit on the same axis in the
board and in alarms. The database and the audit table remain the record; this is the
signal. Pure payload builder plus a fire-and-forget publisher."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("api")
NAMESPACE = "Argus/Reviews"


def metric_payload(
    decision: str, officer: str, priority: str | None = None
) -> list[dict]:
    """PutMetricData entries: an accept/reject count by decision, a 1/0 "accepted" value
    for averaging, and the same by report priority when known."""
    accepted = 1.0 if decision == "accepted" else 0.0
    dims = [{"Name": "decision", "Value": decision}]
    data = [
        {
            "MetricName": "ReviewCount",
            "Dimensions": dims,
            "Value": 1.0,
            "Unit": "Count",
        },
        {"MetricName": "Accepted", "Dimensions": [], "Value": accepted, "Unit": "None"},
    ]
    if priority:
        data.append(
            {
                "MetricName": "Accepted",
                "Dimensions": [{"Name": "priority", "Value": priority}],
                "Value": accepted,
                "Unit": "None",
            }
        )
    return data


def publish(decision: str, officer: str, priority: str | None = None) -> bool:
    """Publish the review; never raises (a metric must not fail a review)."""
    if os.getenv("REVIEW_METRICS", "aws").lower() == "off":
        return False
    try:
        import boto3

        boto3.client(
            "cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).put_metric_data(
            Namespace=NAMESPACE, MetricData=metric_payload(decision, officer, priority)
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("review metric not published: %s", e)
        return False
