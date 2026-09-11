"""CI gates what it claims to gate, and holds no long-lived credential (I14, I7).

`cdk synth` ran with `|| true` because the environment lookups needed AWS credentials,
so an infrastructure regression rendered no template and CI stayed green; it surfaced at
deploy time instead, after a rollback. The evals workflow held an access key pair in
repository secrets: a credential the stack cannot rotate, that does not expire, and that
is enough to review findings.
"""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
EVALS = yaml.safe_load((ROOT / ".github/workflows/evals.yml").read_text())
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
EVALS_TEXT = (ROOT / ".github/workflows/evals.yml").read_text()


def steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def test_synth_is_a_gate_not_a_wish():
    synth = [s for s in steps(CI, "checks") if s.get("name") == "cdk synth"]
    assert synth, "the synth step moved"
    assert "|| true" not in synth[0]["run"]
    assert "cdk synth --all" in synth[0]["run"]


def test_the_context_fixture_makes_synth_credential_free():
    fixture = json.loads((ROOT / "infra/cdk/cdk.context.ci.json").read_text())
    keys = [k for k in fixture if not k.startswith("_")]
    assert any(k.startswith("availability-zones:") for k in keys)
    assert any("PrefixList" in k for k in keys)
    # Keyed to the CI account the workflow sets, not this deployment's.
    assert all("123456789012" in k for k in keys)
    synth = next(s for s in steps(CI, "checks") if s.get("name") == "cdk synth")
    assert synth["env"]["CDK_DEFAULT_ACCOUNT"] == "123456789012"
    assert "cdk.context.ci.json" in synth["run"]


def test_the_templates_are_asserted_after_they_are_built():
    names = [s.get("name") for s in steps(CI, "checks")]
    assert "template assertions" in names
    assert names.index("cdk synth") < names.index("template assertions")


def test_dependencies_are_audited():
    assert any(s.get("name") == "pip-audit" for s in steps(CI, "checks"))
    assert "pip-audit" in CI_TEXT


def test_images_are_scanned():
    assert "image-scan" in CI["jobs"]
    assert "trivy" in CI_TEXT.lower()


def test_the_evals_workflow_holds_no_access_key():
    assert "AWS_ACCESS_KEY_ID" not in EVALS_TEXT
    assert "AWS_SECRET_ACCESS_KEY" not in EVALS_TEXT
    assert "aws-access-key-id" not in EVALS_TEXT


def test_the_evals_workflow_uses_the_oidc_token():
    assert EVALS["jobs"]["node-evals"]["permissions"]["id-token"] == "write"
    assert "EVAL_CI_ROLE_ARN" in EVALS_TEXT
    assert "role-chaining: true" in EVALS_TEXT


def test_the_stack_creates_the_ci_role_scoped_to_this_repository():
    stack = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()
    assert "OpenIdConnectProvider" in stack
    assert "token.actions.githubusercontent.com" in stack
    block = stack.split('"CiOperator"', 1)[1][:1500]
    assert "repo:{github_repo}:*" in block, "the role must not trust every repository"
    assert '"sts.amazonaws.com"' in stack


def test_dependabot_covers_every_requirements_file():
    """Only the tracked ones: build output under cdk.out is a copy, not a source."""
    import subprocess

    cfg = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    pip = next(u for u in cfg["updates"] if u["package-ecosystem"] == "pip")
    covered = {d.lstrip("/") for d in pip["directories"]}
    tracked = subprocess.run(
        ["git", "ls-files", "*requirements*.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked, "no requirements files tracked; is this a git checkout?"
    for rel in tracked:
        directory = str(Path(rel).parent)
        assert directory in covered, rel


def test_dependabot_groups_the_telemetry_packages_together():
    """The OTel SDK and its instrumentation must move as one, or they stop matching."""
    cfg = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    pip = next(u for u in cfg["updates"] if u["package-ecosystem"] == "pip")
    assert "opentelemetry" in pip["groups"]


def test_the_unit_suite_still_needs_nothing_but_pytest():
    """Template assertions run as their own step because they need synth output."""
    unit = [
        s for s in steps(CI, "checks") if s.get("run", "").startswith("pytest -q -m")
    ]
    assert unit and "not integration" in unit[0]["run"]
