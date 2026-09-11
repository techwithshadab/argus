"""One value, many files: the scenario clock and the Nova tiers cannot drift apart (I15).

`SCENARIO_END` is defined in ten places and each Nova model id in several more. Nothing
checked they agreed, so a change to one was a silent disagreement everywhere else: the
API's idea of "now" against the agents', or a tier's model against the one the stack
grants IAM access to. These read the files rather than importing them, so the suite
still runs with only pytest, pyyaml and pydantic.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_END = "2026-09-01T08:00:00Z"
FAST = "us.amazon.nova-lite-v1:0"
STANDARD = "us.amazon.nova-2-lite-v1:0"
STRONG = "us.amazon.nova-pro-v1:0"
CDK = json.loads((ROOT / "infra/cdk/cdk.json").read_text())["context"]


def read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_every_scenario_clock_default_agrees():
    assert f"SCENARIO_END={SCENARIO_END}" in read(".env.example")
    assert f'_env("SCENARIO_END", "{SCENARIO_END}")' in read("agents/shared/config.py")
    for rel in ("mcp-servers/servers/ais.py", "services/api/main.py"):
        assert f'os.getenv("SCENARIO_END", "{SCENARIO_END}")' in read(rel), rel
    assert CDK["scenarioEnd"] == SCENARIO_END
    for stack in ("agents_stack.py", "platform_stack.py"):
        assert f'try_get_context("scenarioEnd") or "{SCENARIO_END}"' in read(
            f"infra/cdk/stacks/{stack}"
        ), stack


def test_the_imagery_server_reads_the_same_clock():
    """It was the only server without one, so its pass estimates used the wall clock."""
    src = read("mcp-servers/servers/imagery.py")
    assert f'os.getenv("SCENARIO_END", "{SCENARIO_END}")' in src


def test_every_compose_service_that_needs_the_clock_has_it():
    compose = read("docker-compose.yml")
    stated = re.findall(r"SCENARIO_END: \$\{SCENARIO_END:-([^}]+)\}", compose)
    assert stated, "no service sets the scenario clock"
    assert set(stated) == {SCENARIO_END}, stated


def test_no_second_scenario_date_hides_anywhere():
    for rel in (".env.example", "docker-compose.yml", "infra/cdk/cdk.json"):
        found = set(re.findall(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ", read(rel)))
        assert found <= {SCENARIO_END}, (rel, found)


def test_the_nova_tiers_are_the_same_everywhere():
    cfg = read("agents/shared/config.py")
    assert f'"fast": "{FAST}"' in cfg
    assert f'"standard": "{STANDARD}"' in cfg
    assert f'"strong": "{STRONG}"' in cfg
    assert f'"bedrock": "{STRONG}"' in cfg


def test_the_committed_context_names_models_the_tiers_use():
    tiers = {FAST, STANDARD, STRONG}
    for key in ("bedrockModelId", "judgeModelId", "harnessModelId"):
        assert CDK[key] in tiers, (key, CDK[key])


def test_the_cdk_fallbacks_match_the_committed_context():
    """A fallback that disagrees with cdk.json is a different model on a bare synth."""
    src = read("infra/cdk/stacks/agents_stack.py")
    for key in ("bedrockModelId", "judgeModelId", "harnessModelId"):
        assert f'try_get_context("{key}") or "{CDK[key]}"' in src, key


def test_production_stays_nova_only():
    """ADR-0002: the IAM vendor allowlist is `amazon` alone."""
    assert CDK["bedrockModelVendors"] == "amazon"
    assert CDK["allowExternalModelProviders"] is False
    for key in ("bedrockModelId", "judgeModelId", "harnessModelId"):
        assert CDK[key].startswith("us.amazon.nova"), key


def test_every_tier_is_priced():
    """The cost panel keys on a short name; a rename would silently zero it."""
    prices = read("services/api/main.py")
    for full in (FAST, STANDARD, STRONG):
        short = full.split("amazon.")[1].rsplit("-v1:0", 1)[0]
        assert f'"{short}"' in prices, short
