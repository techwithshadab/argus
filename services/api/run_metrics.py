"""Investigation outcomes as CloudWatch metrics (namespace Argus/Investigations): completed
runs, Investigator branches that degraded (one branch failed and the case went on with the
other, see the orchestrator's investigator_branch) and model-tier escalations. The manifest
in Aurora is the record; this is the signal the `argus-degraded-branches` alarm watches.
Pure payload builder plus a fire-and-forget publisher, like review_metrics."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("api")
NAMESPACE = "Argus/Investigations"


def degraded_nodes(manifest: dict) -> list[str]:
    return [n.get("node", "?") for n in manifest.get("nodes", []) if n.get("degraded")]


def escalations(manifest: dict) -> list[str]:
    return [
        n.get("node", "?") for n in manifest.get("nodes", []) if n.get("escalated_to")
    ]


def metric_payload(manifest: dict) -> list[dict]:
    return [
        {"MetricName": "Completed", "Value": 1.0, "Unit": "Count"},
        {
            "MetricName": "DegradedBranches",
            "Value": float(len(degraded_nodes(manifest))),
            "Unit": "Count",
        },
        {
            "MetricName": "Escalations",
            "Value": float(len(escalations(manifest))),
            "Unit": "Count",
        },
    ]


def publish(manifest: dict) -> bool:
    """Publish the run; never raises (a metric must not fail a completion)."""
    if os.getenv("REVIEW_METRICS", "aws").lower() == "off":
        return False
    try:
        import boto3

        boto3.client(
            "cloudwatch", region_name=os.getenv("AWS_REGION", "us-east-1")
        ).put_metric_data(Namespace=NAMESPACE, MetricData=metric_payload(manifest))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("run metric not published: %s", e)
        return False
