"""Provider selection logic. Pure functions only: no provider SDK is imported."""

import sys

import pytest

sys.path.insert(0, "agents")
from shared.config import (  # noqa: E402
    DEFAULT_MODEL_IDS,
    PROVIDERS,
    Settings,
    resolve_model_id,
    resolve_provider,
)
from shared.models import model_spec  # noqa: E402


def _settings(**over) -> Settings:
    base = dict(
        aws_region="us-east-1", temperature=0.1, max_tokens=4096, guardrail_id=""
    )
    return Settings(**{**base, **over})


def test_provider_validation():
    assert resolve_provider("") == "bedrock"
    assert resolve_provider(" OpenAI ") == "openai"
    with pytest.raises(ValueError):
        resolve_provider("azure")


def test_model_id_resolution():
    assert resolve_model_id("bedrock", "", "") == DEFAULT_MODEL_IDS["bedrock"]
    assert resolve_model_id("bedrock", "", "us.anthropic.x") == "us.anthropic.x"
    assert (
        resolve_model_id("openai", "", "us.anthropic.x") == DEFAULT_MODEL_IDS["openai"]
    )
    assert resolve_model_id("gemini", "gemini-2.5-flash", "") == "gemini-2.5-flash"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_spec_for_every_provider(provider):
    spec = model_spec(
        _settings(model_provider=provider, model_id=DEFAULT_MODEL_IDS[provider])
    )
    assert spec["provider"] == provider
    assert spec["strands"]["model_id"] == DEFAULT_MODEL_IDS[provider]
    assert spec["langchain"]["model"] == DEFAULT_MODEL_IDS[provider]


def test_bedrock_guardrail_and_sampling():
    spec = model_spec(
        _settings(
            model_provider="bedrock",
            model_id="us.amazon.nova-pro-v1:0",
            guardrail_id="g1",
            guardrail_version="2",
        )
    )
    assert spec["strands"]["temperature"] == 0.1  # Nova accepts sampling parameters
    assert spec["strands"]["guardrail_id"] == "g1"
    assert "guardrail_id" not in spec["langchain"]


def test_claude5_and_reasoning_models_get_no_temperature():
    anth = model_spec(_settings(model_provider="anthropic", model_id="claude-opus-5"))
    assert "params" not in anth["strands"] and "temperature" not in anth["langchain"]
    old = model_spec(
        _settings(model_provider="anthropic", model_id="claude-sonnet-4-6")
    )
    assert old["strands"]["params"] == {"temperature": 0.1}
    oai = model_spec(_settings(model_provider="openai", model_id="gpt-5"))
    assert oai["strands"]["params"] == {"max_completion_tokens": 4096}
    oai4 = model_spec(_settings(model_provider="openai", model_id="gpt-4.1"))
    assert oai4["strands"]["params"]["temperature"] == 0.1
    gem = model_spec(_settings(model_provider="gemini", model_id="gemini-2.5-pro"))
    assert gem["langchain"]["max_output_tokens"] == 4096
