"""Turn evals/cases.yaml into an AgentCore Evaluations dataset (predefined scenarios) and
start a batch evaluation over a time window of live sessions (ADR-0012).

  python evals/agentcore_dataset.py dataset            # writes evals/agentcore_dataset.json
  python evals/agentcore_dataset.py batch --hours 24   # scores the last 24 h of sessions
                                                       #   with the online config's evaluators

The dataset feeds the AgentCore dataset runners (on-demand or batch) for pre/post
comparison of a prompt or model change; the batch command is the managed "experiment"
over what really ran. The local `node_evals.py --gate` remains the CI gate."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).parent


def scenarios_from_cases(cases: dict) -> dict:
    """Predefined scenarios: one per Investigator and Tasking case, with the tools we expect
    the agent to call (any order) and the expected content as natural-language assertions."""
    scenarios = []
    inv = cases.get("investigator", {})
    for c in inv.get("cases", []):
        msg = (
            f"Scope: {c['scope']}\nVessel MMSI: {c['mmsi']}\nTrigger: {c.get('trigger', 'manual')}\n"
            f"Triggering alert: {json.dumps(c.get('alert', {}))}\n"
            "Return the InvestigationFindings JSON for your scope only."
        )
        expected_tools = (
            ["registry___lookup_vessel", "registry___sanctions_screen"]
            if c["scope"] == "identity"
            else ["ais___get_vessel_track", "ais___find_ais_gaps"]
        )
        assertions = [
            f"The findings mention {k if isinstance(k, str) else ' or '.join(k)}"
            for k in c.get("expect", [])
        ]
        assertions += [
            f"The findings do not claim {k}" for k in c.get("expect_absent", [])
        ]
        assertions.append("Every evidence entry cites a tool that was actually called")
        scenarios.append(
            {
                "scenario_id": f"investigator-{c['name']}",
                "turns": [{"input": msg}],
                "expected_trajectory": expected_tools,
                "assertions": assertions,
                "metadata": {"suite": "investigator", "mmsi": str(c["mmsi"])},
            }
        )
    for c in cases.get("tasking", {}).get("cases", []):
        msg = (
            f"Vessel MMSI: {c['mmsi']}\nBehaviour summary: {c['behaviour']}\n"
            f"Evidence gap (position and time where evidence is missing): {c['gap']}\n"
            "Decide whether collection would help and, if so, propose it. Return the TaskingRecommendation JSON."
        )
        want = (
            "recommends collection"
            if c.get("expect_recommended")
            else "does not recommend collection"
        )
        scenarios.append(
            {
                "scenario_id": f"tasking-{c['name']}",
                "turns": [{"input": msg}],
                "expected_trajectory": ["imagery___search_sentinel_scenes"],
                "assertions": [
                    f"The recommendation {want}",
                    "The output is a TaskingRecommendation JSON object",
                ],
                "metadata": {"suite": "tasking", "mmsi": str(c["mmsi"])},
            }
        )
    return {"scenarios": scenarios}


def write_dataset(cases_path: Path, out: Path) -> int:
    data = scenarios_from_cases(yaml.safe_load(cases_path.read_text()))
    out.write_text(json.dumps(data, indent=2) + "\n")
    return len(data["scenarios"])


def start_batch(hours: float, region: str, config_name: str = "argus_agents") -> str:
    import boto3

    c = boto3.client("bedrock-agentcore-control", region_name=region)
    configs = c.list_online_evaluation_configs().get(
        "onlineEvaluationConfigs"
    ) or c.list_online_evaluation_configs().get("items", [])
    cfg = next(x for x in configs if x.get("onlineEvaluationConfigName") == config_name)
    arn = cfg.get("onlineEvaluationConfigArn") or cfg["arn"]
    end = datetime.now(UTC)
    name = f"argus_batch_{int(time.time())}"
    d = boto3.client("bedrock-agentcore", region_name=region)
    resp = d.start_batch_evaluation(
        batchEvaluationName=name,
        description=f"Argus sessions of the last {hours:g} h",
        dataSourceConfig={
            "onlineEvaluationConfigSource": {
                "onlineEvaluationConfigArn": arn,
                "timeRange": {
                    "startTime": end - timedelta(hours=hours),
                    "endTime": end,
                },
            }
        },
    )
    return resp.get("batchEvaluationId") or resp.get("batchEvaluationArn", name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["dataset", "batch"])
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--out", default=str(HERE / "agentcore_dataset.json"))
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--region", default="us-east-1")
    a = ap.parse_args()
    if a.command == "dataset":
        n = write_dataset(Path(a.cases), Path(a.out))
        print(f"wrote {a.out} ({n} scenarios)")
        return 0
    print("batch evaluation:", start_batch(a.hours, a.region))
    return 0


if __name__ == "__main__":
    sys.exit(main())
