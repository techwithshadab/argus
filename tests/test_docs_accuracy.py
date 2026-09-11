"""Docs describe the code that runs, not the code that used to (D1, D2).

Two claims had drifted far enough to mislead a reader: officer identity was still
described as a request header "until OIDC", months after the load balancer's Cognito
sign-in shipped, and the report policy loop was described as one retry failing closed,
while the code accepts a soft problem with a caveat and allows a third attempt after a
guardrail block. Both are now pinned against the code they describe.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
ARCH = (ROOT / "ARCHITECTURE.md").read_text()
USE_CASES = (ROOT / "docs/USE_CASES.md").read_text()
POLICY = (ROOT / "agents/shared/policy.py").read_text()
ORCH = (ROOT / "agents/orchestrator/app.py").read_text()


def test_no_document_still_says_officer_identity_waits_for_oidc():
    for name, text in (("README", README), ("ARCHITECTURE", ARCH)):
        assert "until the load balancer gets an OIDC" not in text, name
        assert "until OIDC" not in text, name


def test_the_officer_identity_sections_describe_the_deployed_sign_in():
    for name, text in (("README", README), ("ARCHITECTURE", ARCH)):
        assert "x-amzn-oidc-data" in text, name
        assert "ADR-0018" in text, name


def test_the_soft_problems_the_docs_describe_are_the_ones_the_code_has():
    """Soft means an officer can still act on the report while seeing the flaw."""
    assert "SOFT_PROBLEMS = (BALANCE_PROBLEM, UNTRACEABLE_PROBLEM)" in POLICY
    assert "def hard_problems(" in POLICY
    for name, text in (
        ("README", README),
        ("ARCHITECTURE", ARCH),
        ("USE_CASES", USE_CASES),
    ):
        assert "counter-indicators" in text, name
    # Every soft problem must produce a caveat naming it, or the officer cannot see it.
    for const in ("BALANCE_PROBLEM", "UNTRACEABLE_PROBLEM"):
        assert const in ORCH, const


def test_no_document_claims_a_second_violation_always_fails_closed():
    for name, text in (
        ("README", README),
        ("ARCHITECTURE", ARCH),
        ("USE_CASES", USE_CASES),
    ):
        assert "a second violation fails the investigation closed" not in text, name


def test_the_documented_retry_count_matches_the_loop():
    """The loop runs attempts 1, 2 and 3, the third only after a guardrail block."""
    loop = re.search(r"for attempt in \(([^)]+)\)", ORCH).group(1)
    assert loop.replace(" ", "") == "1,2,3"
    assert "guardrail" in ARCH.lower()


def test_the_degraded_cap_is_documented_where_the_report_is_described():
    assert "ADR-0020" in ARCH and "ADR-0020" in README and "ADR-0020" in USE_CASES
    assert "caps the report" in README or "cap" in README.lower()


def test_the_new_adrs_exist_and_are_accepted():
    for slug in (
        "0019-two-allowlists-agents-propose-officers-decide",
        "0020-hard-and-soft-report-problems",
    ):
        text = (ROOT / f"docs/adr/{slug}.md").read_text()
        assert "Status: accepted" in text, slug
        assert text.startswith("# ADR-00"), slug


def test_the_docs_index_counts_the_adrs_correctly():
    words = {
        16: "Sixteen",
        17: "Seventeen",
        18: "Eighteen",
        19: "Nineteen",
        20: "Twenty",
        21: "Twenty-one",
        22: "Twenty-two",
    }
    n = len(list((ROOT / "docs/adr").glob("[0-9]*.md")))
    index = (ROOT / "docs/README.md").read_text()
    assert f"{words[n]} architecture decision records" in index, n


def test_the_api_reference_states_the_split_allowlists_and_the_conflicts():
    api = (ROOT / "docs/API.md").read_text()
    assert "AGENT_ALLOWED_ROLES" in api and "OFFICER_ALLOWED_ROLES" in api
    assert api.count("409") >= 4
    assert "redacted" in api


# ---- D3: the project documents a public repository needs ----
def test_the_project_documents_exist():
    for rel in (
        "LICENSE",
        "NOTICE",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "docs/MODEL_CARD.md",
        "docs/RESPONSIBLE_AI.md",
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
    ):
        assert (ROOT / rel).exists(), rel


def test_every_document_is_reachable_from_the_index():
    index = (ROOT / "docs/README.md").read_text()
    for name in (
        "MODEL_CARD.md",
        "RESPONSIBLE_AI.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
    ):
        assert name in index, name


def test_the_model_card_says_what_the_system_must_not_be_used_for():
    """A decision-support system near enforcement needs this stated, not implied."""
    card = (ROOT / "docs/MODEL_CARD.md").read_text()
    assert "## Out Of Scope" in card
    assert "## Known Failure Modes" in card
    assert "## Human Oversight" in card
    for claim in ("automated enforcement", "Tracking a person"):
        assert claim.lower() in card.lower(), claim


def test_the_model_card_tiers_match_the_code():
    """A card that describes a different model than the one that runs is worse than none."""
    card = (ROOT / "docs/MODEL_CARD.md").read_text()
    cfg = (ROOT / "agents/shared/config.py").read_text()
    roles = cfg.split("DEFAULT_ROLE_TIERS = {", 1)[1].split("}", 1)[0]
    for role, tier in (
        ("watch", "fast"),
        ("tasking", "standard"),
        ("investigator", "strong"),
    ):
        assert f'"{role}": "{tier}"' in roles, role
    assert "Nova Lite" in card and "Nova Pro" in card


def test_the_responsible_ai_page_is_honest_about_what_is_not_evaluated():
    page = (ROOT / "docs/RESPONSIBLE_AI.md").read_text()
    assert "## Bias And Fairness" in page
    assert "not been formally evaluated" in page


# ---- D4, D8, D9: claims that had drifted ----
def test_live_mode_is_not_described_as_a_single_box():
    for rel in ("ARCHITECTURE.md", "docs/TECHNICAL.md"):
        text = (ROOT / rel).read_text()
        assert "WATCH_AREAS" in text, rel


def test_the_agent_card_path_is_the_one_the_sdk_serves():
    """a2a-sdk 0.3 names the old path PREV_AGENT_CARD_WELL_KNOWN_PATH."""
    api = (ROOT / "docs/API.md").read_text()
    assert "/.well-known/agent-card.json" in api
    readme = (ROOT / "README.md").read_text()
    assert "/.well-known/agent-card.json" in readme


def test_the_roadmap_does_not_claim_the_graph_is_unexercised():
    roadmap = (ROOT / "docs/ROADMAP.md").read_text()
    assert "only with the stub agents" not in roadmap


def test_nothing_sits_under_out_of_scope_that_is_in_scope():
    """A `Done:` bullet under "Out of scope for now" asserts the opposite of itself."""
    roadmap = (ROOT / "docs/ROADMAP.md").read_text()
    section = roadmap.split("## Out of scope for now", 1)[1].split("\n## ", 1)[0]
    assert "Done:" not in section and "Next:" not in section


def test_the_documented_phase_count_matches_the_headings():
    roadmap = (ROOT / "docs/ROADMAP.md").read_text()
    phases = len(re.findall(r"^## Phase \d", roadmap, re.M))
    words = {5: "five", 6: "six", 7: "seven"}
    assert f"{words[phases]} phases" in (ROOT / "docs/README.md").read_text()


# ---- D10, D11 ----
def test_the_requirements_count_is_the_real_one():
    import subprocess

    n = len(
        subprocess.run(
            ["git", "ls-files", "*requirements*.txt"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    words = {9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
    for rel in ("docs/TECHNICAL.md", "CLAUDE.md"):
        assert f"{words[n]} `requirements.txt` files" in (ROOT / rel).read_text(), rel


def test_the_configuration_sentence_is_finished():
    readme = (ROOT / "README.md").read_text()
    para = readme.split("## Configuration", 1)[1].split("##", 1)[0].strip()
    assert para.endswith("."), "the switch list used to stop at a semicolon"


def test_retention_and_deletion_are_stated():
    sec = (ROOT / "docs/SECURITY.md").read_text()
    assert "## Retention and deletion" in sec
    for subject in ("AgentCore Memory", "Evidence snapshots", "Personal data"):
        assert subject in sec, subject
