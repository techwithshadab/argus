"""Provider-neutral model factories.

MODEL_PROVIDER selects the backend; MODEL_ID the model. The Strands agents call
``strands_model()`` and the LangGraph agent calls ``langchain_model()``; everything else
in the agents is provider-agnostic.

Provider SDKs are imported lazily so this module (and its pure helpers) can be imported
without any of them installed.
"""

from __future__ import annotations

import dataclasses
import logging
import os

from shared.config import (
    API_KEY_ENV,
    Settings,
    resolve_role_tier,
    resolve_tier_model,
    settings,
)

log = logging.getLogger("argus.models")

# Claude 5-family models reject sampling parameters (temperature/top_p) and run adaptive
# thinking by default; older Claude models accept them.
_NO_SAMPLING_CLAUDE = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable",
    "claude-mythos",
)
# OpenAI reasoning models only accept the default temperature.
_NO_SAMPLING_OPENAI_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def claude_accepts_sampling(model_id: str) -> bool:
    return not any(marker in model_id for marker in _NO_SAMPLING_CLAUDE)


def openai_accepts_sampling(model_id: str) -> bool:
    return not model_id.startswith(_NO_SAMPLING_OPENAI_PREFIXES)


def model_spec(s: Settings = settings) -> dict:
    """Constructor kwargs for the selected provider, framework-neutral and side-effect free.

    Returns {"provider", "strands": {...}, "langchain": {...}} where the two dicts are the
    exact keyword arguments for the Strands and LangChain model classes respectively."""
    p, mid, temp, max_tokens = s.model_provider, s.model_id, s.temperature, s.max_tokens
    if p == "bedrock":
        sampling = {"temperature": temp} if claude_accepts_sampling(mid) else {}
        strands = {
            "model_id": mid,
            "region_name": s.aws_region,
            "max_tokens": max_tokens,
            **sampling,
        }
        if s.guardrail_id:
            strands.update(
                guardrail_id=s.guardrail_id,
                guardrail_version=s.guardrail_version,
                guardrail_trace="enabled",
            )
        langchain = {
            "model": mid,
            "region_name": s.aws_region,
            "max_tokens": max_tokens,
            **sampling,
        }
        if s.guardrail_id:
            langchain["guardrail_config"] = {
                "guardrailIdentifier": s.guardrail_id,
                "guardrailVersion": s.guardrail_version,
                "trace": "enabled",
            }
    elif p == "anthropic":
        sampling = {"temperature": temp} if claude_accepts_sampling(mid) else {}
        strands = {"model_id": mid, "max_tokens": max_tokens}
        if sampling:
            strands["params"] = sampling
        langchain = {"model": mid, "max_tokens": max_tokens, **sampling}
    elif p == "openai":
        sampling = {"temperature": temp} if openai_accepts_sampling(mid) else {}
        strands = {
            "model_id": mid,
            "params": {"max_completion_tokens": max_tokens, **sampling},
        }
        langchain = {"model": mid, "max_tokens": max_tokens, **sampling}
    elif p == "gemini":
        strands = {
            "model_id": mid,
            "params": {"temperature": temp, "max_output_tokens": max_tokens},
        }
        langchain = {"model": mid, "temperature": temp, "max_output_tokens": max_tokens}
    else:  # unreachable: Settings validates the provider
        raise ValueError(f"unknown provider {p!r}")
    return {"provider": p, "strands": strands, "langchain": langchain}


def ensure_api_key(s: Settings = settings) -> None:
    """Make sure the provider SDK will find its key. Locally the key comes from the env;
    on AWS it is read once from Secrets Manager (MODEL_API_KEY_SECRET_ARN)."""
    var = API_KEY_ENV.get(s.model_provider)
    if var is None or os.getenv(var):
        return
    if s.model_provider == "gemini" and os.getenv("GEMINI_API_KEY"):
        return
    if not s.model_api_key_secret_arn:
        raise RuntimeError(
            f"MODEL_PROVIDER={s.model_provider} needs {var} in the environment "
            "(or MODEL_API_KEY_SECRET_ARN on AWS)"
        )
    import boto3

    secret = boto3.client("secretsmanager", region_name=s.aws_region).get_secret_value(
        SecretId=s.model_api_key_secret_arn
    )
    os.environ[var] = secret["SecretString"].strip()
    log.info("loaded %s from Secrets Manager", var)


def for_tier(
    role: str, tier: str | None = None, s: Settings = settings
) -> tuple[Settings, str]:
    """Settings with the model id for a role's tier (or an explicit tier), plus the tier used.
    MODEL_ID set explicitly pins every tier to that model (the pre-tier behaviour)."""
    tier = tier or resolve_role_tier(role, os.getenv(f"MODEL_TIER_{role.upper()}", ""))
    pinned = os.getenv("MODEL_ID", "")
    model_id = pinned or resolve_tier_model(
        s.model_provider, tier, os.getenv(f"MODEL_ID_{tier.upper()}", "")
    )
    return dataclasses.replace(s, model_id=model_id), tier


UNAVAILABLE = (
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ServiceQuotaExceededException",
    "ModelNotReadyException",
    "ModelTimeoutException",
    "InternalServerException",
)


def model_unavailable(exc: BaseException) -> bool:
    """A provider-side availability failure (throttled, not ready, timed out) rather
    than a bad request: worth one escalation to the next tier, whose model has its own
    quota. Pure: reads the exception's class, botocore error code and message."""
    resp = getattr(exc, "response", None)
    code = (
        str((resp.get("Error") or {}).get("Code", "")) if isinstance(resp, dict) else ""
    )
    text = f"{type(exc).__name__} {code} {exc}"
    return any(k in text for k in UNAVAILABLE) or "throttl" in text.lower()


def _bedrock_retries():
    """Adaptive retries for throttling: four workers and two Investigator branches can hit
    the Nova per-model TPS at once, and a throttled call must not become a lost branch."""
    from botocore.config import Config

    return Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=300)


def strands_model(
    s: Settings = settings, *, role: str | None = None, tier: str | None = None
):
    """Strands ``Model`` for the configured provider. With ``role`` (and optionally ``tier``) the
    model comes from the tier table (phase 4)."""
    if role or tier:
        s, _ = for_tier(role or "standard", tier, s)
    ensure_api_key(s)
    spec = model_spec(s)
    kwargs = spec["strands"]
    log.info("model provider=%s model=%s", spec["provider"], s.model_id)
    if spec["provider"] == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(boto_client_config=_bedrock_retries(), **kwargs)
    if spec["provider"] == "anthropic":
        from strands.models.anthropic import AnthropicModel

        return AnthropicModel(**kwargs)
    if spec["provider"] == "openai":
        from strands.models.openai import OpenAIModel

        return OpenAIModel(**kwargs)
    from strands.models.gemini import GeminiModel

    return GeminiModel(**kwargs)


def langchain_model(
    s: Settings = settings, *, role: str | None = None, tier: str | None = None
):
    """LangChain chat model for the configured provider (tier-aware like strands_model)."""
    if role or tier:
        s, _ = for_tier(role or "standard", tier, s)
    ensure_api_key(s)
    spec = model_spec(s)
    kwargs = spec["langchain"]
    log.info("model provider=%s model=%s", spec["provider"], s.model_id)
    if spec["provider"] == "bedrock":
        from langchain_aws import ChatBedrockConverse

        return ChatBedrockConverse(config=_bedrock_retries(), **kwargs)
    if spec["provider"] == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(**kwargs)
    if spec["provider"] == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(**kwargs)
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(**kwargs)
