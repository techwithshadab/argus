"""The alarm inventory, the Grafana rules and the RUNBOOK say the same thing (A9, B7, B9)."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ALARMS = (ROOT / "infra/cdk/stacks/alarms.py").read_text()
RUNBOOK = (ROOT / "docs/RUNBOOK.md").read_text()
PLATFORM = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()

# Every name the module builds, with the labels the platform stack passes in.
EXPECTED = [
    "argus-jobs-oldest-message",
    "argus-sweeps-oldest-message",
    "argus-jobs-backlog",
    "argus-sweeps-backlog",
    "argus-jobs-dlq",
    "argus-sweeps-dlq",
    "argus-sweep-schedule-dlq",
    "argus-public-alb-5xx",
    "argus-internal-alb-5xx",
    "argus-public-alb-target-5xx",
    "argus-internal-alb-target-5xx",
    "argus-api-down",
    "argus-ui-down",
    "argus-grafana-down",
    "argus-worker-down",
    "argus-sweep-worker-down",
    "argus-collector-down",
    "argus-ais-replay-down",
    "argus-aurora-cpu",
    "argus-aurora-capacity",
    "argus-aurora-local-storage",
    "argus-waf-blocked",
    "argus-degraded-branches",
    "argus-feed-stalled",
    "argus-sign-in-failures",
    "argus-alb-auth-errors",
    "argus-deployment-rollbacks",
    "argus-sweeps-raising-nothing",
    "argus-nat-<n>-port-allocation",
    "argus-nat-<n>-packets-dropped",
    "argus-eval-<suite>",
]


def test_runbook_documents_every_alarm():
    missing = [n for n in EXPECTED if f"`{n}`" not in RUNBOOK]
    assert not missing, missing
    # The alarms section only: later sections name other argus-prefixed things
    # (snapshot identifiers, for one) that are not alarms.
    section = RUNBOOK.split("## Alarms")[1].split("\n## ")[0]
    stale = re.findall(r"`(argus-[a-z0-9<>-]+)`", section)
    unknown = sorted(set(stale) - set(EXPECTED) - {"argus-alerts"})
    assert not unknown, f"RUNBOOK names alarms the stack does not create: {unknown}"


def test_alarm_patterns_exist_in_the_module():
    for pattern in (
        '"argus-{label}-oldest-message"',
        '"argus-{label}-backlog"',
        '"argus-{label}-dlq"',
        '"argus-{label}-alb-5xx"',
        '"argus-{label}-alb-target-5xx"',
        '"argus-{label}-down"',
        '"argus-aurora-cpu"',
        '"argus-aurora-capacity"',
        '"argus-aurora-local-storage"',
        '"argus-nat-{i}-{what}"',
        '"argus-eval-{suite}"',
    ):
        assert pattern in ALARMS, pattern
    # Alarm and recovery both notify, and missing eval data never pages.
    assert "add_ok_action" in ALARMS
    assert "TreatMissingData.NOT_BREACHING" in ALARMS


def test_platform_stack_wires_alarms_and_the_ui_dependency():
    assert 'topic_name="argus-alerts"' in PLATFORM
    assert "ui_svc.node.add_dependency(api_svc)" in PLATFORM
    for label in ('"worker"', '"sweep-worker"', '"collector"'):
        assert f"plain_services[{label}]" in PLATFORM
    assert 'dlqs={**dlqs, "sweep-schedule": schedule_dlq}' in PLATFORM


def test_grafana_rules_cover_eval_gate_and_scrape_loss():
    raw = (ROOT / "observability/aws/alerting.yaml").read_text()
    doc = yaml.safe_load(
        raw.replace("${ALERT_SNS_TOPIC_ARN}", "arn").replace("${AWS_REGION}", "r")
    )
    rules = {r["uid"]: r for g in doc["groups"] for r in g["rules"]}
    assert "argus-eval-regression" in rules and "argus-api-scrape-lost" in rules
    assert (
        "argus_eval_passed"
        in rules["argus-eval-regression"]["data"][0]["model"]["expr"]
    )
    assert (
        'up{job="argus-api"}'
        in rules["argus-api-scrape-lost"]["data"][0]["model"]["expr"]
    )
    for r in rules.values():
        assert r["condition"] == "C"
        assert [d["refId"] for d in r["data"]] == ["A", "B", "C"]
    assert doc["contactPoints"][0]["receivers"][0]["settings"]["topic_arn"] == "arn"


def test_eval_push_publishes_the_gate():
    src = (ROOT / "evals/node_evals.py").read_text()
    assert '"gate_passed": 1.0 if passed else 0.0' in src


def test_alerts_topic_lets_cloudwatch_alarms_publish():
    """enforce_ssl replaces the default topic policy; without an explicit allow for the
    CloudWatch Alarms service principal every alarm action fails (seen on deploy)."""
    src = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()
    topic = src[src.index('topic_name="argus-alerts"') :]
    grant = topic[: topic.index("add_subscription")]
    assert 'iam.ServicePrincipal("cloudwatch.amazonaws.com")' in grant
    assert '"sns:Publish"' in grant
    assert '"aws:SourceAccount"' in grant


def test_the_documented_alarm_count_matches_the_synthesized_stack():
    """Six documents state a number. It said 31 long after the stack built 35.

    Counted from the template rather than from arithmetic over this module, so it stays
    true however the alarms are grouped. Skipped when there is no synth output.
    """
    import json

    out = ROOT / "infra/cdk/cdk.out/argus-platform.template.json"
    if not out.exists():
        pytest.skip("no synth output; run `make synth` first")
    built = sum(
        1
        for r in json.loads(out.read_text())["Resources"].values()
        if r["Type"] == "AWS::CloudWatch::Alarm"
    )
    for doc in ("README.md", "ARCHITECTURE.md", "docs/SECURITY.md", "docs/AUDIT.md"):
        text = (ROOT / doc).read_text()
        for stated in re.findall(r"(\d+) (?:CloudWatch )?alarms", text):
            assert int(stated) == built, (
                f"{doc} says {stated}; the stack builds {built}"
            )
        # A spelled-out count slipped past a digits-only check once.
        spelled = re.findall(
            r"\b(?:twenty|thirty|forty|fifty)[- ]?"
            r"(?:one|two|three|four|five|six|seven|eight|nine)?"
            r" (?:CloudWatch )?alarms",
            text,
            re.I,
        )
        assert not spelled, (
            f"{doc} spells out an alarm count; use digits so it is checked"
        )
