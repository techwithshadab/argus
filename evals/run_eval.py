"""Score watch-agent alerts against scenario ground truth."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from scoring import overlap, raised_since, score  # noqa: F401  (pure, unit-tested)

_client = None


def http():
    """One HTTP client per run. EVAL_AUTH=aws-iam attaches this principal's caller token
    (the API on AWS admits the roles in TOOL_ALLOWED_ROLES, e.g. `argus-operator`; the
    public load balancer lets bearer requests on /api/* past the officer sign-in), and
    EVAL_TLS_VERIFY=false accepts the self-signed watch-floor certificate."""
    global _client
    if _client is None:
        auth = None
        if os.getenv("EVAL_AUTH", "").lower() == "aws-iam":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
            from shared.caller_auth import CallerIdentityAuth

            auth = CallerIdentityAuth(os.getenv("AWS_REGION", "us-east-1"))
        verify = os.getenv("EVAL_TLS_VERIFY", "true").lower() != "false"
        _client = httpx.Client(auth=auth, verify=verify, timeout=60)
    return _client


def push_to_cloudwatch(result: dict, region: str = "us-east-1") -> None:
    """Optional: watch-suite scores as CloudWatch metrics (Argus/Evals)."""
    if os.getenv("EVAL_PUSH", "").lower() != "cloudwatch":
        return
    import boto3

    data = [
        {
            "MetricName": k,
            "Dimensions": [{"Name": "suite", "Value": "watch"}],
            "Value": float(v),
            "Unit": "None",
        }
        for k, v in result.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    if data:
        boto3.client("cloudwatch", region_name=region).put_metric_data(
            Namespace="Argus/Evals", MetricData=data
        )


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument(
        "--since",
        default=os.getenv("EVAL_SINCE"),
        help="score only alerts created at or after this ISO timestamp "
        "(default: everything, which measures the deployment's whole history)",
    )
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="run a sweep first and score only what it raises",
    )
    args = ap.parse_args()
    since = args.since
    if args.sweep:
        since = datetime.now(UTC).isoformat()
        http().post(f"{args.api}/sweep?hours=12")
        # The sweep is queued, then the Watch agent works through its candidates.
        time.sleep(float(os.getenv("EVAL_SWEEP_WAIT_S", "90")))
    truth = http().get(f"{args.api}/ground-truth").json()
    alerts = raised_since(http().get(f"{args.api}/alerts").json(), since)
    result = score(truth, alerts)
    if since:
        result["since"] = since
    print(json.dumps(result, indent=1))
    push_to_cloudwatch(result)
