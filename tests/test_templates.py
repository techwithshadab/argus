"""Assertions on the synthesized CloudFormation, so an infrastructure regression fails CI.

`cdk synth` used to run with `|| true` because the environment lookups needed AWS
credentials, which meant a broken stack rendered no template and CI stayed green; the
failure surfaced at deploy time instead, after a rollback. The lookups are now a
committed fixture and synth is a real gate, and these read the templates it produces.

They skip when `infra/cdk/cdk.out` is absent, so the unit suite still runs with only
pytest, pyyaml and pydantic installed and no Node or CDK.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infra/cdk/cdk.out"

pytestmark = pytest.mark.skipif(
    not (OUT / "argus-platform.template.json").exists(),
    reason="no synth output; run `make synth` first",
)


def template(stack: str) -> dict:
    return json.loads((OUT / f"argus-{stack}.template.json").read_text())


def resources(stack: str, kind: str) -> dict:
    return {k: v for k, v in template(stack)["Resources"].items() if v["Type"] == kind}


def test_the_records_survive_a_stack_deletion():
    """I1: the cluster snapshots and carries deletion protection; the archive is retained."""
    for _, r in resources("data", "AWS::RDS::DBCluster").items():
        assert r.get("DeletionPolicy") == "Snapshot"
        assert r.get("UpdateReplacePolicy") == "Snapshot"
        assert r["Properties"].get("DeletionProtection") is True
    archive = [
        r
        for k, r in resources("data", "AWS::S3::Bucket").items()
        if "archive" in k.lower()
    ]
    assert archive and all(r.get("DeletionPolicy") == "Retain" for r in archive)


def test_services_that_carry_traffic_keep_a_task_through_a_deploy():
    """I3: the API, the UI and the collector run two tasks and never deploy through zero."""
    two = 0
    for _, r in resources("platform", "AWS::ECS::Service").items():
        desired = r["Properties"].get("DesiredCount")
        healthy = (
            r["Properties"]
            .get("DeploymentConfiguration", {})
            .get("MinimumHealthyPercent")
        )
        if desired == 2:
            two += 1
            assert healthy == 100, r["Properties"].get("ServiceName")
    assert two == 3, "expected the API, the UI and the collector to run two tasks"


def test_the_ingest_task_is_a_singleton():
    """Two ingest tasks would double-write every position."""
    services = resources("platform", "AWS::ECS::Service")
    replay = [k for k in services if "aisreplay" in k.lower().replace("-", "")]
    assert replay, "the replay service moved"
    assert all(services[k]["Properties"]["DesiredCount"] == 1 for k in replay)


def test_the_api_separates_agent_and_officer_roles():
    """ADR-0019: one list per question, and the operator is not an agent role."""
    env = {}
    for _, r in resources("platform", "AWS::ECS::TaskDefinition").items():
        for c in r["Properties"].get("ContainerDefinitions", []):
            vals = {
                e.get("Name"): e.get("Value")
                for e in c.get("Environment", [])
                if isinstance(e.get("Value"), str)
            }
            if "OFFICER_ALLOWED_ROLES" in vals:
                env = vals
                break
    assert env, "no container carries the allowlists"
    agents = set(env["AGENT_ALLOWED_ROLES"].split(","))
    officers = set(env["OFFICER_ALLOWED_ROLES"].split(","))
    assert officers == {"argus-operator"}
    assert not (agents & officers), "an agent role may not take an officer action"


def test_the_budget_exists_and_notifies_before_the_month_ends():
    """I13: a forecast notification arrives while the spend can still be changed."""
    budgets = resources("platform", "AWS::Budgets::Budget")
    assert budgets, "no budget in the platform stack"
    b = next(iter(budgets.values()))["Properties"]
    assert b["Budget"]["TimeUnit"] == "MONTHLY"
    kinds = {
        n["Notification"]["NotificationType"] for n in b["NotificationsWithSubscribers"]
    }
    assert "FORECASTED" in kinds and "ACTUAL" in kinds


def test_traces_are_sampled_rather_than_indexed_whole():
    """I13: 100% indexing is a bill, not a signal."""
    body = (OUT / "argus-platform.template.json").read_text()
    assert '"DesiredSamplingPercentage": 100' not in body


def test_the_feed_and_gate_alarms_breach_when_they_stop_reporting():
    """I4, I12: a metric that stops arriving must not read as healthy."""
    alarms = {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in resources("platform", "AWS::CloudWatch::Alarm").values()
    }
    assert alarms["argus-feed-stalled"]["TreatMissingData"] == "breaching"
    gates = [n for n in alarms if n.startswith("argus-eval-")]
    assert gates and all(alarms[n]["TreatMissingData"] == "breaching" for n in gates)
    for name in ("argus-sign-in-failures", "argus-deployment-rollbacks"):
        assert name in alarms


def test_every_alarm_notifies_the_topic():
    for r in resources("platform", "AWS::CloudWatch::Alarm").values():
        assert r["Properties"].get("AlarmActions"), r["Properties"]["AlarmName"]


def test_the_operator_role_is_not_open_to_the_whole_account_without_mfa():
    """I7: this role may review findings and approve collection requests."""
    roles = {
        k: r
        for k, r in resources("platform", "AWS::IAM::Role").items()
        if r["Properties"].get("RoleName") == "argus-operator"
    }
    assert roles, "the operator role moved"
    doc = json.dumps(
        next(iter(roles.values()))["Properties"]["AssumeRolePolicyDocument"]
    )
    assert "MultiFactorAuthPresent" in doc or "arn:aws:iam" in doc


def test_the_records_survive_the_region_they_live_in():
    """I2: automated backups are seven days and same-region; the plan is the long tail."""
    vaults = resources("data", "AWS::Backup::BackupVault")
    plans = resources("data", "AWS::Backup::BackupPlan")
    assert vaults and plans
    rules = next(iter(plans.values()))["Properties"]["BackupPlan"]["BackupPlanRule"]
    by_name = {r["RuleName"]: r["Lifecycle"] for r in rules}
    assert by_name["daily-35d"]["DeleteAfterDays"] == 35
    assert by_name["monthly-7y"]["DeleteAfterDays"] >= 7 * 365
    # Cold storage needs at least 90 days of retention, so it belongs on the monthly rule.
    assert "MoveToColdStorageAfterDays" not in by_name["daily-35d"]
    assert by_name["monthly-7y"]["MoveToColdStorageAfterDays"] >= 90


def test_the_vault_key_outlives_a_destroy():
    """A vault under the destroyable data key is unreadable a week after a destroy."""
    keys = resources("data", "AWS::KMS::Key")
    assert any(v.get("DeletionPolicy") == "Retain" for v in keys.values())


def test_the_zone_count_is_the_committed_default():
    """I5 is implemented but deliberately not the default.

    `-c maxAzs=3` spreads the agents, which they need: AgentCore supports only some
    zones and the intersection with the two CDK picks is often one. But subnet CIDRs
    are allocated per zone in order, so raising it renumbers them and replaces the
    existing agent subnets, which AgentCore's network interfaces pin. It stays at 2
    so a running stack is never broken by an ordinary deploy; take the third zone on a
    fresh VPC.
    """
    committed = json.loads((ROOT / "infra/cdk/cdk.json").read_text())["context"][
        "maxAzs"
    ]
    assert committed == 2, "raising this in cdk.json replaces the live agent subnets"
    zones = {
        r["Properties"]["AvailabilityZone"]
        for r in resources("network", "AWS::EC2::Subnet").values()
        if isinstance(r["Properties"].get("AvailabilityZone"), str)
    }
    assert len(zones) == committed, zones
    # The capability is still there, and the warning with it.
    stack = (ROOT / "infra/cdk/stacks/network_stack.py").read_text()
    assert 'try_get_context("maxAzs")' in stack
    assert "replaces the existing agent subnets" in stack


def test_the_edge_keeps_what_it_refused():
    """I19: the rolling sample had usually rolled by the time an alarm was investigated."""
    logging = resources("platform", "AWS::WAFv2::LoggingConfiguration")
    assert logging, "the web ACL logs nothing"
    props = next(iter(logging.values()))["Properties"]
    redacted = {list(f.values())[0]["Name"] for f in props["RedactedFields"]}
    # `/api/*` bearer requests pass through this balancer, so tokens must not be logged.
    assert {"authorization", "x-amzn-oidc-data"} <= redacted


def test_logs_outlive_a_long_weekend():
    """I17: three days against a seven-year audit posture and a runbook that says
    "check the task's logs"."""
    groups = [
        r
        for r in resources("platform", "AWS::Logs::LogGroup").values()
        if r["Properties"].get("LogGroupName") == "/argus/services"
    ]
    assert groups and groups[0]["Properties"]["RetentionInDays"] >= 30


def test_grafana_cannot_query_every_log_group_in_the_account():
    """I11: only StartQuery can be scoped; the other two are matched by query id."""
    body = (OUT / "argus-platform.template.json").read_text()
    assert "logs:StartQuery" in body
    policies = [
        r
        for r in resources("platform", "AWS::IAM::Policy").values()
        if "logs:StartQuery" in json.dumps(r["Properties"]["PolicyDocument"])
    ]
    assert policies
    for p in policies:
        for stmt in p["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if "logs:StartQuery" in actions:
                assert stmt["Resource"] != "*", "StartQuery must name the log groups"


def test_the_collector_is_not_pulled_from_docker_hub_at_task_start():
    """I18: every service depends on the collector, so a rate limit stopped the platform."""
    body = (OUT / "argus-platform.template.json").read_text()
    assert "otel/opentelemetry-collector-contrib" not in body


def test_the_sign_in_has_an_alarm_of_its_own():
    """I10: a broken Cognito integration locks officers out while everything looks healthy."""
    names = {
        r["Properties"]["AlarmName"]
        for r in resources("platform", "AWS::CloudWatch::Alarm").values()
    }
    assert "argus-alb-auth-errors" in names


def test_the_documented_image_count_is_the_real_one():
    """CLAUDE.md warns what happens when the build context is wrong; the number must be true."""
    words = {
        13: "thirteen",
        14: "fourteen",
        15: "fifteen",
        16: "sixteen",
        17: "seventeen",
    }
    built = 0
    for stack in ("argus-platform", "argus-agents", "argus-data", "argus-network"):
        manifest = OUT / f"{stack}.assets.json"
        if manifest.exists():
            built += len(json.loads(manifest.read_text()).get("dockerImages", {}))
    assert built, "no image assets; run `make synth` first"
    assert f"{words[built]} images" in (ROOT / "CLAUDE.md").read_text(), built
