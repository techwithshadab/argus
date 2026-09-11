"""The provenance manifest records which Bedrock Guardrail guarded the run.

Dependency-free (`sys.path.insert(0, "agents")` matches tests/test_schemas.py: inside
containers `agents/shared` is copied to `/app/shared`).

Both directions are asserted: a configured guardrail is recorded exactly, and an unconfigured
one yields `{}` rather than a default that an auditor could not tell from a real reading.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from shared.provenance import guardrail, manifest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_guardrail_env(monkeypatch):
    monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("BEDROCK_GUARDRAIL_VERSION", raising=False)


def test_guardrail_is_recorded_from_the_runtime_environment(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "oop4nkv1vyo8")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "1")
    assert guardrail() == {"id": "oop4nkv1vyo8", "version": "1"}


def test_guardrail_is_empty_when_none_is_configured():
    """Local compose has no guardrail; the field must read as absent, never as a default."""
    assert guardrail() == {}


def test_guardrail_version_falls_back_to_draft_only_when_the_id_is_set(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "abc123")
    assert guardrail() == {"id": "abc123", "version": "DRAFT"}


def test_an_id_alone_is_not_enough_to_claim_a_guardrail(monkeypatch):
    """A version without an id must not produce a half-recorded guardrail."""
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "7")
    assert guardrail() == {}


def test_manifest_carries_the_guardrail(monkeypatch):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "oop4nkv1vyo8")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "1")
    m = manifest([{"node": "report", "model_id": "x"}], {"ais": "1.0"})
    assert m["guardrail"] == {"id": "oop4nkv1vyo8", "version": "1"}


def test_manifest_still_has_the_key_when_unguarded():
    """The key is always present so a reader never has to distinguish missing from empty."""
    m = manifest([], {})
    assert m["guardrail"] == {}


def test_adding_the_guardrail_did_not_disturb_the_existing_manifest_shape():
    """The SLI and cost readers walk manifest['nodes']; a new top-level key must not move it."""
    nodes = [{"node": "report", "model_id": "m", "attempts": 2}]
    m = manifest(nodes, {"ais": "1.0"})
    for key in (
        "manifest_version",
        "created_at",
        "code_revision",
        "schema_version",
        "prompts",
        "nodes",
        "mcp_servers",
    ):
        assert key in m, key
    assert m["nodes"] == nodes
    assert m["mcp_servers"] == {"ais": "1.0"}


def test_extra_still_merges_over_the_base(monkeypatch):
    m = manifest([], {}, {"trigger": "officer", "alert_id": "a1"})
    assert m["trigger"] == "officer"
    assert m["alert_id"] == "a1"
