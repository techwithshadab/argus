"""Alert evidence cites tools as `server.tool` whatever the model calls them (A1).

The Watch agent reaches its tools through the AgentCore gateway on AWS and directly
in compose, and the model echoes whichever spelling it was given. Alerts raised on
AWS therefore cited `geo___point_in_zones`: a form no officer recognises, that no
scorer keyed on the dotted name can match, and that differs from the same sweep run
locally. Only the Investigator path canonicalised its citations.

Sources are canonicalised in pure code so the alert payload is the same in both
modes, and entries that cite nothing an officer could follow are dropped rather
than stored.
"""

import sys

sys.path.insert(0, "agents")


def candidate():
    return {
        "mmsi": 232003453,
        "kind": "ais_gap",
        "evidence": [
            {
                "source": "ais.find_ais_gaps",
                "summary": "6 h gap",
                "reference": "ais_gap 232003453 2026-09-01T02:00:00+00:00",
            }
        ],
    }


def test_gateway_names_are_rewritten_to_the_dotted_form():
    from shared.sweep import alert_evidence

    out = alert_evidence(
        candidate(), [{"source": "geo___point_in_zones", "summary": "in zone"}]
    )
    assert [e["source"] for e in out] == ["ais.find_ais_gaps", "geo.point_in_zones"]


def test_every_spelling_the_transports_produce_lands_on_one_form():
    from shared.sweep import alert_evidence

    spellings = [
        "geo___point_in_zones",  # gateway
        "geo_point_in_zones",  # strands
        "geo.point_in_zones",  # already canonical
    ]
    for name in spellings:
        out = alert_evidence(candidate(), [{"source": name, "summary": "in zone"}])
        assert out[-1]["source"] == "geo.point_in_zones", name


def test_the_candidates_own_evidence_is_kept_first_and_untouched():
    from shared.sweep import alert_evidence

    c = candidate()
    out = alert_evidence(c, [{"source": "geo___point_in_zones", "summary": "in zone"}])
    assert out[0] == c["evidence"][0]


def test_entries_that_cite_nothing_are_dropped():
    from shared.sweep import alert_evidence

    extra = [
        {"source": "", "summary": "no source"},
        {"summary": "no source key"},
        {"source": "geo___point_in_zones"},  # no summary
        {"source": "geo___point_in_zones", "summary": "   "},
        "not a dict",
        None,
        42,
    ]
    assert alert_evidence(candidate(), extra) == candidate()["evidence"]


def test_a_repeated_citation_is_not_stored_twice():
    from shared.sweep import alert_evidence

    same = {
        "source": "ais___find_ais_gaps",
        "summary": "6 h gap",
        "reference": "ais_gap 232003453 2026-09-01T02:00:00+00:00",
    }
    assert alert_evidence(candidate(), [same]) == candidate()["evidence"]


def test_none_and_an_empty_list_are_handled():
    from shared.sweep import alert_evidence

    assert alert_evidence(candidate(), None) == candidate()["evidence"]
    assert alert_evidence(candidate(), []) == candidate()["evidence"]
    assert alert_evidence({}, None) == []


def test_the_watch_tool_uses_the_pure_assembler():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "agents/watch/app.py").read_text()
    raise_alert = src.split("def raise_alert(", 1)[1].split("\n@tool")[0]
    assert "alert_evidence(c, extra_evidence)" in raise_alert
    assert "isinstance(e, dict)" not in raise_alert, "the inline filter should be gone"
