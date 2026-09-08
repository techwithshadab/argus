"""Pure pieces added in the 'use the stack properly' round: prior context for the branches,
report payload coercion, AIS static data mapping, SLI aggregation, review metrics, and the
guardrail wiring in the model spec."""

import json
import sys
from dataclasses import replace

sys.path.insert(0, "agents")
sys.path.insert(0, "services/ais-replay")
sys.path.insert(0, "services/api")

from shared.config import Settings  # noqa: E402
from shared.graph import prior_context_block, report_payload  # noqa: E402
from shared.models import model_spec  # noqa: E402


def test_prior_context_is_labelled_and_capped():
    assert prior_context_block("") == ""
    block = prior_context_block("x" * 5000, limit=100)
    assert "may be stale" in block and "do not cite it as evidence" in block
    assert len(block) < 300 and block.rstrip().endswith("…")


def test_report_payload_restores_identifiers_and_lists():
    out = report_payload(
        {"headline": "h", "priority": "low"}, 511666006, "MERIDIAN STAR"
    )
    assert out["mmsi"] == 511666006 and out["vessel_name"] == "MERIDIAN STAR"
    assert out["indicators"] == [] and out["evidence"] == []
    wrapped = report_payload({"properties": {"headline": {"value": "h"}}}, 1, "")
    assert wrapped["headline"] == "h" and wrapped["vessel_name"] == "1"


def test_guardrail_reaches_both_frameworks():
    s = replace(
        Settings(),
        model_provider="bedrock",
        model_id="us.amazon.nova-pro-v1:0",
        guardrail_id="abc123",
        guardrail_version="1",
    )
    spec = model_spec(s)
    assert spec["strands"]["guardrail_id"] == "abc123"
    assert spec["langchain"]["guardrail_config"]["guardrailIdentifier"] == "abc123"
    assert spec["langchain"]["guardrail_config"]["trace"] == "enabled"


def test_ais_static_rows_map_flag_type_and_dimensions():
    from aisstatic import static_rows, upsert_registry_sql, upsert_vessel_sql

    msg = {
        "MessageType": "ShipStaticData",
        "MetaData": {"ShipName": "ENERGEAN STAR "},
        "Message": {
            "ShipStaticData": {
                "UserID": 538009102,
                "ImoNumber": 9301234,
                "CallSign": "V7AB2",
                "Type": 83,
                "Dimension": {"A": 120, "B": 45, "C": 10, "D": 12},
                "Destination": "LIMASSOL",
                "MaximumStaticDraught": 9.5,
            }
        },
    }
    vessel, registry = static_rows(msg)
    assert vessel["flag"] == "MH" and vessel["ship_type"] == "tanker"
    assert vessel["length_m"] == 165.0 and vessel["name"] == "ENERGEAN STAR"
    assert (
        vessel["meta"]["destination"] == "LIMASSOL"
        and vessel["meta"]["source"] == "aisstream"
    )
    assert registry["mmsi"] == 538009102 and registry["imo"] == 9301234
    sql, params = upsert_vessel_sql(vessel)
    assert "is_synthetic = false" in sql and params[0] == 538009102
    sql2, _ = upsert_registry_sql(registry)
    assert "DO NOTHING" in sql2
    assert static_rows({"Message": {}}) == (None, None)


def test_ais_static_unknown_mid_gives_no_flag():
    from aisstatic import flag_from_mmsi, ship_type_text

    assert flag_from_mmsi(999000001) is None
    assert flag_from_mmsi("511666006") == "PW"
    assert ship_type_text(30) == "fishing" and ship_type_text(None) is None


def test_node_stats_and_prometheus_lines():
    import sli_pure

    manifests = [
        {
            "nodes": [
                {
                    "node": "report",
                    "model_id": "m",
                    "attempts": 2,
                    "usage": {"inputTokens": 10, "outputTokens": 5},
                },
                {
                    "node": "tasking",
                    "attempts": 1,
                    "error": "x",
                    "policy_problems": ["bad"],
                },
            ]
        },
        {
            "nodes": [
                {
                    "node": "report",
                    "model_id": "m",
                    "attempts": 1,
                    "usage": {"inputTokens": 1},
                }
            ]
        },
    ]
    st = sli_pure.node_stats(manifests)
    assert (
        st["tokens"][("report", "m", "input")] == 11
        and st["tokens"][("report", "m", "output")] == 5
    )
    assert st["escalations"] == 1 and st["rejections"] == 1 and st["nodes"] == 3
    lines = sli_pure.prometheus_lines(st)
    assert any(
        line.startswith(
            'argus_node_tokens_24h{node="report",model="m",kind="input"} 11'
        )
        for line in lines
    )
    assert "argus_node_escalations_24h 1" in lines


def test_usage_delta_turns_running_totals_into_per_call_usage():
    from shared.graph import usage_delta

    first = usage_delta(
        {}, {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110}
    )
    second = usage_delta(
        first and {"inputTokens": 100, "outputTokens": 10, "totalTokens": 110},
        {"inputTokens": 250, "outputTokens": 30, "totalTokens": 280},
    )
    assert (
        first["inputTokens"] == 100
        and second["inputTokens"] == 150
        and second["outputTokens"] == 20
    )


def test_grafana_sns_contact_point_uses_topic_arn():
    """Grafana's SNS integration validates `topic_arn`; a mistyped key fails alerting
    provisioning, which stops the Grafana process, which fails the ECS deployment."""
    import yaml

    doc = yaml.safe_load(open("observability/aws/alerting.yaml"))
    receivers = [r for cp in doc["contactPoints"] for r in cp["receivers"]]
    sns = [r for r in receivers if r["type"] == "sns"]
    assert sns and all("topic_arn" in r["settings"] for r in sns)
    assert {cp["name"] for cp in doc["contactPoints"]} >= {
        p["receiver"] for p in doc["policies"]
    }


def test_runtime_session_ids_fold_back_to_the_investigation():
    import re

    from shared.graph import RUNTIME_SESSION_RE, runtime_session_id

    sys.path.insert(0, "services/api")
    import runtime_session

    iid = "c3219a3f-be39-46cf-a700-cc26e2bda558"
    for role in ("orchestrator", "identity", "behaviour", "tasking"):
        sid = runtime_session_id(iid, role)
        assert len(sid) >= 33 and re.sub(RUNTIME_SESSION_RE, r"\1", sid) == iid
    assert runtime_session.runtime_session_id(iid) == runtime_session_id(
        iid, "orchestrator"
    )
    assert len(runtime_session_id("", "x")) >= 33
    # Parallel branches never share a runtime session (AgentCore answers 409).
    assert runtime_session_id(iid, "identity") != runtime_session_id(iid, "behaviour")


def test_guardrail_block_and_memory_helpers():
    from shared.graph import guardrail_blocked, memory_messages, recall_text

    assert guardrail_blocked("This request was blocked by the Argus guardrail.")
    assert not guardrail_blocked('{"headline": "x"}') and not guardrail_blocked("")
    msgs = memory_messages("Assessment: loitering", "inv-1", 538009102)
    assert [r for _, r in msgs] == ["USER", "ASSISTANT"]
    assert "538009102" in msgs[0][0] and msgs[1][0] == "Assessment: loitering"
    out = recall_text(["fact a", ""], ["raw 1", "fact a", "raw 2"], limit=2)
    assert out == "fact a\nraw 1"


def test_policy_allows_sharing_with_partners_in_the_plural():
    from shared.policy import check_actions

    ok = [
        "Share findings with relevant partners for coordinated action.",
        "Share the assessment with partner agencies.",
        "Continue to monitor the vessel.",
        "Request updated information on the vessel's ownership and operator details.",
        "Propose further investigation if additional suspicious behavior is detected.",
    ]
    assert check_actions(ok) == []
    assert check_actions(["Seize the vessel and arrest the crew."])


def test_mcp_health_url_only_swaps_the_path():
    from shared.config import health_url

    assert health_url("http://mcp-ais:8000/mcp") == "http://mcp-ais:8000/health"
    assert (
        health_url("http://alb.internal:8001/mcp/") == "http://alb.internal:8001/health"
    )
    assert health_url("http://mcp-geo:8000/health") == "http://mcp-geo:8000/health"


def test_report_policy_requires_balance():
    from shared.policy import validate_report

    base = {
        "recommended_actions": ["Continue to monitor."],
        "indicators": ["AIS gap"],
        "evidence": [{"source": "ais.find_ais_gaps", "reference": "x"}],
    }
    assert any(
        p.startswith("no counter-indicators or information gaps stated")
        for p in validate_report(base, {"ais.find_ais_gaps"})
    )
    assert (
        validate_report(
            {**base, "information_gaps": ["no imagery"]}, {"ais.find_ais_gaps"}
        )
        == []
    )


def test_alert_kinds_are_normalised_and_sources_prefixed():
    from shared.graph import findings_payload, normalise_kind

    assert normalise_kind("mmsi_conflict") == "mmsi_spoof"
    assert (
        normalise_kind("AIS gap") == "ais_gap"
        and normalise_kind("loitering") == "loitering"
    )
    servers = {"find_ais_gaps": "ais", "lookup_vessel": "registry"}
    out = findings_payload(
        {
            "assessment": "x",
            "evidence": [
                {"source": "find_ais_gaps", "reference": "g1"},
                {"source": "ais_find_ais_gaps", "reference": "g2"},
                {"source": "registry.lookup_vessel", "reference": "r1"},
                {"source": "made_up", "reference": "m"},
            ],
        },
        1,
        "behaviour",
        servers,
    )
    assert [e["source"] for e in out["evidence"]] == [
        "ais.find_ais_gaps",
        "ais.find_ais_gaps",
        "registry.lookup_vessel",
        "made_up",
    ]


def test_balance_is_a_soft_policy_problem():
    from shared.policy import BALANCE_PROBLEM, hard_problems

    assert hard_problems([BALANCE_PROBLEM]) == []
    assert hard_problems([BALANCE_PROBLEM, "action not allowed: seize"]) == [
        "action not allowed: seize"
    ]


def test_rendezvous_matches_either_party():
    sys.path.insert(0, "evals")
    from scoring import score

    truth = [
        {
            "kind": "rendezvous",
            "mmsi": 511666006,
            "details": {"with_mmsi": 422888009},
            "started_at": "2026-09-01T02:00:00+00:00",
            "ended_at": "2026-09-01T03:30:00+00:00",
        }
    ]
    alert = {
        "mmsi": 422888009,
        "kind": "rendezvous",
        "started_at": "2026-09-01T02:10:00+00:00",
        "ended_at": "2026-09-01T02:50:00+00:00",
    }
    assert score(truth, [alert])["recall"] == 1.0


def test_context_excludes_keep_only_what_the_image_copies(tmp_path):
    sys.path.insert(0, "infra/cdk")
    from stacks.assets import context_excludes

    for f in (
        "services/api/main.py",
        "services/api/Dockerfile",
        "services/ui/index.html",
        "agents/shared/graph.py",
        "evals/results/x.json",
        "README.md",
    ):
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x")
    ex = context_excludes(str(tmp_path), ["services/api/Dockerfile", "services/api"])
    assert (
        "services/ui" in ex and "agents" in ex and "evals" in ex and "README.md" in ex
    )
    assert not any(e.startswith("services/api") for e in ex)
    assert "services" not in ex


def test_judge_consensus_averages_dimensions():
    sys.path.insert(0, "evals")
    from scoring import judge_consensus

    out = judge_consensus(
        [
            {"bluf": 4, "balance": 2, "comment": "a"},
            {"bluf": 5, "balance": 4, "comment": "b"},
            {"bluf": 3, "balance": "n/a"},
        ]
    )
    assert out["bluf"] == 4.0 and out["balance"] == 3.0
    assert out["comment"] == "a" and out["samples"] == 3
    from scoring import rubric_average

    assert rubric_average(out) == 3.5


def test_keyword_alternatives_and_unscored_precision():
    sys.path.insert(0, "evals")
    from scoring import keyword_hits, score

    assert (
        keyword_hits("Flag Palau, owner Halcyon", ["Halcyon", ["PW", "Palau"]]) == 1.0
    )
    assert keyword_hits("nothing", [["PW", "Palau"]]) == 0.0
    truth = [
        {
            "kind": "ais_gap",
            "mmsi": 1,
            "started_at": "2026-09-01T06:00:00Z",
            "ended_at": "2026-09-01T07:00:00Z",
        }
    ]
    alerts = [
        {
            "kind": "ais_gap",
            "mmsi": 1,
            "started_at": "2026-09-01T06:10:00Z",
            "ended_at": "2026-09-01T06:50:00Z",
        },
        {
            "kind": "zone_incursion",
            "mmsi": 2,
            "started_at": "2026-09-01T06:10:00Z",
            "ended_at": "2026-09-01T06:50:00Z",
        },
        {
            "kind": "ais_gap",
            "mmsi": 3,
            "started_at": "2026-09-01T01:00:00Z",
            "ended_at": "2026-09-01T02:00:00Z",
        },
    ]
    s = score(truth, alerts)
    assert s["recall"] == 1.0 and s["precision"] == 0.5 and s["unscored"] == 1


def test_sweep_candidates_are_built_from_detectors_and_deduplicated():
    from shared.sweep import candidates_from, render_candidates

    det = {
        "gaps": {
            "window": ["2026-08-31T20:00:00+00:00", "2026-09-01T08:00:00+00:00"],
            "gaps": [
                {
                    "mmsi": 511666006,
                    "name": "MERIDIAN STAR",
                    "gap_start": "2026-09-01T06:26:00+00:00",
                    "gap_end": "2026-09-01T07:27:00+00:00",
                    "gap_minutes": 61.0,
                    "distance_nm": 12.3,
                    "implied_speed_kn": 12.1,
                    "last_lat": 34.05,
                    "last_lon": 33.6,
                }
            ],
            "still_dark": [
                {
                    "mmsi": 999000001,
                    "name": None,
                    "gap_start": "2026-09-01T05:00:00+00:00",
                    "gap_minutes": 180.0,
                    "last_lat": 34.0,
                    "last_lon": 33.0,
                }
            ],
        },
        "conflicts": {
            "conflicts": [
                {
                    "mmsi": 636222002,
                    "conflicting_reports": 40,
                    "first_seen": "2026-09-01T00:03:00+00:00",
                    "last_seen": "2026-09-01T07:57:00+00:00",
                    "max_jump_nm": 30.2,
                    "track_centroids": [],
                }
            ]
        },
        "rendezvous": {
            "rendezvous": [
                {
                    "mmsi_a": 422888009,
                    "mmsi_b": 511666006,
                    "name_a": "NAVAND 3",
                    "name_b": "MERIDIAN STAR",
                    "started_at": "2026-09-01T02:49:00+00:00",
                    "ended_at": "2026-09-01T03:29:00+00:00",
                    "samples": 40,
                    "lat": 34.35,
                    "lon": 34.35,
                }
            ]
        },
        "loitering": {"loitering": []},
        "incursions": {"incursions": []},
    }
    open_alerts = [
        {
            "mmsi": 636222002,
            "kind": "mmsi_conflict",
            "started_at": "2026-09-01T00:00:00+00:00",
            "ended_at": "2026-09-01T07:59:00+00:00",
        }
    ]
    cands = candidates_from(det, open_alerts)
    kinds = [c["kind"] for c in cands]
    assert kinds == ["ais_gap", "ais_gap", "rendezvous"]  # spoof already open
    gap = cands[0]
    assert gap["mmsi"] == 511666006 and gap["started_at"].startswith("2026-09-01T06:26")
    assert gap["evidence"][0]["source"] == "ais.find_ais_gaps"
    assert cands[1]["ended_at"] == "2026-09-01T08:00:00+00:00"  # still dark: window end
    assert cands[2]["partner_mmsi"] == 511666006
    ids = [c["id"] for c in cands]
    assert len(set(ids)) == 3 and all("-c" in i for i in ids)
    view = render_candidates(cands)
    assert view[0]["window"][0] == gap["started_at"] and "summary" in view[0]
    assert candidates_from({}, []) == []


def test_parse_sweep_request_reads_hours_and_until():
    import importlib.util

    src = open("agents/watch/app.py").read()
    start = src.index("def parse_sweep_request")
    end = src.index("\n\n\n", start)
    ns: dict = {"re": importlib.import_module("re")}
    exec(src[start:end], ns)  # noqa: S102  (the module itself imports strands)
    f = ns["parse_sweep_request"]
    assert f("Sweep the last 12 hours of AIS and raise alerts.") == (12.0, None)
    assert f("Sweep the last 1.5 h until 2026-09-01T08:00:00Z") == (
        1.5,
        "2026-09-01T08:00:00Z",
    )
    assert f("please sweep") == (12.0, None)


def test_rank_candidates_caps_and_prefers_zones_and_spoofing():
    from shared.sweep import rank_candidates

    cands = [
        {
            "kind": "ais_gap",
            "mmsi": 1,
            "summary": "12-minute AIS gap; no declared zone",
        },
        {
            "kind": "zone_incursion",
            "mmsi": 2,
            "summary": "40 reports inside Corridor K",
        },
        {"kind": "mmsi_spoof", "mmsi": 3, "summary": "two centroids"},
        {"kind": "ais_gap", "mmsi": 4, "summary": "180-minute AIS gap; last known 1,2"},
    ]
    top, rest = rank_candidates(cands, limit=3)
    assert [c["mmsi"] for c in top] == [3, 4, 2]
    assert [c["mmsi"] for c in rest] == [1]
    assert rank_candidates([], 5) == ([], [])


def test_some_candidates_can_only_be_raised():
    from shared.sweep import non_dismissible

    assert non_dismissible({"kind": "mmsi_spoof", "summary": ""})
    assert non_dismissible(
        {
            "kind": "rendezvous",
            "summary": "…; no declared zone or anchorage at the meeting point",
        }
    )
    assert (
        non_dismissible(
            {"kind": "rendezvous", "summary": "inside Limassol Anchorage (anchorage)"}
        )
        is None
    )
    assert non_dismissible({"kind": "ais_gap", "summary": "12-minute AIS gap"}) is None


def test_tool_names_round_trip_in_both_modes(monkeypatch):
    from shared import tools

    assert tools.split_tool_name("ais___find_ais_gaps") == ("ais", "find_ais_gaps")
    assert tools.split_tool_name("ais_find_ais_gaps") == ("ais", "find_ais_gaps")
    assert tools.split_tool_name("registry.lookup_vessel") == (
        "registry",
        "lookup_vessel",
    )
    assert tools.split_tool_name("find_ais_gaps") == (None, "find_ais_gaps")
    assert tools.canonical_source("ais___find_ais_gaps") == "ais.find_ais_gaps"
    assert (
        tools.canonical_source("find_ais_gaps", {"find_ais_gaps": "ais"})
        == "ais.find_ais_gaps"
    )
    assert tools.canonical_source("made_up") == "made_up"
    from dataclasses import replace

    monkeypatch.setattr(tools, "settings", replace(tools.settings, tool_gateway_url=""))
    assert tools.tool_name("ais", "find_ais_gaps") == "find_ais_gaps"
    monkeypatch.setattr(
        tools,
        "settings",
        replace(tools.settings, tool_gateway_url="https://gw.example/mcp"),
    )
    assert tools.tool_name("ais", "find_ais_gaps") == "ais___find_ais_gaps"
    assert tools.health_urls() == []


def test_tool_inventory_matches_the_servers_source():
    """tools.json is generated inside the image (make tools-inventory); it must list every
    @mcp.tool() the servers define, or the registry and the Cedar policies drift."""
    import re

    inv = json.load(open("mcp-servers/tools.json"))
    for server in ("ais", "registry", "geo", "imagery"):
        src = open(f"mcp-servers/servers/{server}.py").read()
        declared = set(re.findall(r"@mcp\.tool\(\)\s*(?:@\w+\s*)*def (\w+)\(", src))
        listed = {t["name"] for t in inv["servers"][server]["tools"]}
        assert declared == listed, (server, declared ^ listed)
        for t in inv["servers"][server]["tools"]:
            assert t["inputSchema"].get("type") == "object"


def test_cedar_policies_cover_each_role_and_only_its_servers():
    sys.path.insert(0, "infra/cdk")
    from stacks.tool_policy import load_inventory, policies, registry_server_descriptor

    inv = load_inventory("mcp-servers/tools.json")
    pol = policies(
        inv,
        "123456789012",
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/g1",
    )
    assert set(pol) == {"watch", "investigator", "tasking"}
    assert 'assumed-role/argus-agent-watch"' in pol["watch"]
    assert "ais___find_ais_gaps" in pol["watch"] and "registry___" not in pol["watch"]
    assert "registry___lookup_vessel" in pol["investigator"]
    assert (
        "imagery___create_tasking_request" in pol["tasking"]
        and "ais___" not in pol["tasking"]
    )
    assert pol["watch"].startswith("permit(") and pol["watch"].rstrip().endswith(");")
    d = registry_server_descriptor(inv, "ais", "https://gw/mcp")
    assert d["name"] == "io.argus/ais" and d["remotes"][0]["type"] == "streamable-http"


def test_review_metric_payload():
    sys.path.insert(0, "services/api")
    import review_metrics

    data = review_metrics.metric_payload("accepted", "officer-1", "high")
    names = [
        (d["MetricName"], tuple((x["Name"], x["Value"]) for x in d["Dimensions"]))
        for d in data
    ]
    assert ("ReviewCount", (("decision", "accepted"),)) in names
    assert ("Accepted", ()) in names and ("Accepted", (("priority", "high"),)) in names
    assert all(d["Value"] == 1.0 for d in data if d["MetricName"] == "Accepted")
    assert review_metrics.metric_payload("rejected", "x")[1]["Value"] == 0.0


def test_prompt_loader_uses_the_file_without_a_managed_arn(monkeypatch):
    from shared import prompts_loader

    monkeypatch.delenv("PROMPT_ARN_WATCH", raising=False)
    prompts_loader._managed.clear()
    text = prompts_loader.load_prompt("watch", scenario_end="2026-09-01T08:00:00Z")
    assert "2026-09-01T08:00:00Z" in text
    assert prompts_loader.prompt_version("watch") == "file"
    assert len(prompts_loader.prompt_hash("watch")) == 16


def test_agentcore_dataset_scenarios_follow_the_schema():
    sys.path.insert(0, "evals")
    import yaml
    from agentcore_dataset import scenarios_from_cases

    data = scenarios_from_cases(yaml.safe_load(open("evals/cases.yaml")))
    ids = [s["scenario_id"] for s in data["scenarios"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 5
    for s in data["scenarios"]:
        assert s["turns"] and all(t["input"] for t in s["turns"])
        assert all("___" in t for t in s["expected_trajectory"])
        assert s["assertions"]
    assert (
        json.load(open("evals/agentcore_dataset.json"))["scenarios"]
        == data["scenarios"]
    )


def test_managed_prompt_placeholders_are_detected():
    sys.path.insert(0, "infra/cdk")
    from stacks.prompt_files import placeholders, prompt_files

    files = prompt_files(".")
    assert set(files) == {
        "watch",
        "investigator",
        "tasking",
        "report",
    }  # orchestrator.md was dead since ADR-0003
    assert placeholders(files["watch"].read_text()) == ["scenario_end"]
    for name, path in files.items():
        for v in placeholders(path.read_text()):
            assert v in ("scenario_end", "schema"), (name, v)
    # doubled braces (literal JSON in the prompts) are not variables
    assert placeholders('{{"alerts_raised": 1}} and {scenario_end}') == ["scenario_end"]


def test_registry_discovery_falls_back_without_a_registry(monkeypatch):
    from shared import discovery

    monkeypatch.delenv("ARGUS_REGISTRY_ID", raising=False)
    monkeypatch.setenv("ARGUS_NO_REGISTRY", "1")
    discovery._cache.clear()
    assert discovery.registry_url("watch") is None
    assert (
        discovery.agent_url("watch", "http://agent-watch:9000")
        == "http://agent-watch:9000"
    )


def test_harness_policy_uses_the_tasking_servers():
    sys.path.insert(0, "infra/cdk")
    from stacks.tool_policy import cedar_permit, load_inventory, tool_ids

    inv = load_inventory("mcp-servers/tools.json")
    actions = [a for s in ("imagery", "geo") for a in tool_ids(inv, s)]
    stmt = cedar_permit(
        "arn:aws:sts::1:assumed-role/argus-harness-tasking", "arn:gw", actions
    )
    assert (
        'AgentCore::IamEntity::"arn:aws:sts::1:assumed-role/argus-harness-tasking"'
        in stmt
    )
    assert all(a.startswith(("imagery___", "geo___")) for a in actions) and actions


def test_report_rubric_carries_a_session_placeholder():
    # infra/cdk/stacks/evaluations.py imports aws_cdk; read the constant from the source.
    src = open("infra/cdk/stacks/evaluations.py").read()
    rubric = src.split('REPORT_RUBRIC = """', 1)[1].split('"""', 1)[0]
    assert "{context}" in rubric


def test_registry_mcp_descriptors_fit_the_server_schema():
    """server.schema.json 2025-12-11: name reverse-DNS with one slash, description 1..100,
    version present, remotes streamable-http. AWS validates it strictly on CreateRegistryRecord."""
    import re

    sys.path.insert(0, "infra/cdk")
    from stacks.tool_policy import load_inventory, registry_server_descriptor

    inv = load_inventory("mcp-servers/tools.json")
    for server in inv["servers"]:
        d = registry_server_descriptor(inv, server, "https://gw.example/mcp")
        assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", d["name"])
        assert 1 <= len(d["description"]) <= 100, (server, len(d["description"]))
        assert d["version"] and d["remotes"][0]["type"] == "streamable-http"


def test_card_url_lists_then_batch_gets_the_card():
    from shared import discovery

    class Fake:
        def list_discoverable_registry_records(self, **kw):
            return {
                "registryRecords": [
                    {"name": "argus-agent-watch", "status": "DRAFT", "recordId": "d"},
                    {
                        "name": "argus-agent-watch",
                        "status": "APPROVED",
                        "recordId": "a",
                    },
                ]
            }

        def batch_get_discoverable_registry_record(self, entries):
            assert entries == [{"registryId": "r", "recordIds": ["a"]}]
            return {
                "registryRecords": [
                    {
                        "descriptors": {
                            "a2aAgentCard": {
                                "data": json.dumps(
                                    {"url": "https://gw/watch/invocations"}
                                )
                            }
                        }
                    }
                ]
            }

    assert (
        discovery.card_url(Fake(), "r", "argus-agent-watch")
        == "https://gw/watch/invocations"
    )
    assert discovery.card_url(Fake(), "r", "argus-agent-nope") is None


def test_choose_report_prompt_attests_the_prompt_that_ran():
    from shared.graph import choose_report_prompt, text_hash

    assert choose_report_prompt("managed", None, None, "3") == ("managed", "3")
    assert choose_report_prompt("managed", {"report_system_prompt": " "}, "7", "3") == (
        "managed",
        "3",
    )
    text, label = choose_report_prompt(
        "managed", {"report_system_prompt": "bundled"}, "7", "3"
    )
    assert (text, label) == ("bundled", "bundle:7") and text_hash(text) != text_hash(
        "managed"
    )


def test_bundle_override_is_off_by_default(monkeypatch):
    import dataclasses

    from shared.config import settings

    assert settings.bundle_override is False
    assert dataclasses.replace(settings, bundle_override=True).bundle_override is True
