"""Environment-driven settings shared by all agents."""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

PROVIDERS = ("bedrock", "anthropic", "openai", "gemini")

# Default model per provider when MODEL_ID is unset.
DEFAULT_MODEL_IDS = {
    "bedrock": "us.amazon.nova-pro-v1:0",
    "anthropic": "claude-opus-5",
    "openai": "gpt-5",
    "gemini": "gemini-2.5-pro",
}

# Model tiers (phase 4): a step's judgement load picks the tier, not the agent's name.
# MODEL_ID_<TIER> overrides a tier's model; MODEL_TIER_<ROLE> overrides a role's tier.
TIERS = ("fast", "standard", "strong")
DEFAULT_TIER_MODELS = {
    # Amazon Nova only on Bedrock (first-party, cheapest, no Claude): Lite for the tool-heavy
    # sweep, Nova 2 Lite for tasking, Pro for the judgement-heavy nodes. Nova Premier is the
    # documented step-up for `strong` (MODEL_ID_STRONG=us.amazon.nova-premier-v1:0).
    "bedrock": {
        "fast": "us.amazon.nova-lite-v1:0",
        "standard": "us.amazon.nova-2-lite-v1:0",
        "strong": "us.amazon.nova-pro-v1:0",
    },
    "anthropic": {
        "fast": "claude-haiku-4-5",
        "standard": "claude-sonnet-5",
        "strong": "claude-opus-5",
    },
    "openai": {"fast": "gpt-5-mini", "standard": "gpt-5", "strong": "gpt-5"},
    "gemini": {
        "fast": "gemini-2.5-flash",
        "standard": "gemini-2.5-pro",
        "strong": "gemini-2.5-pro",
    },
}
DEFAULT_ROLE_TIERS = {
    "watch": "fast",  # high volume, tool-heavy, low reasoning
    "tasking": "standard",
    "investigator": "strong",
    "report": "strong",
}

# Environment variable each provider SDK reads its API key from. Bedrock uses the AWS
# credential chain instead.
API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


def _env(name: str, default: str = "") -> str:
    """os.environ with two defences against .env files edited by hand: an inline comment
    after the value ("value   # note") is dropped, and a value that is only a comment
    ("# note") counts as unset."""
    return clean_env_value(os.getenv(name), default)


def clean_env_value(raw: str | None, default: str = "") -> str:
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return default if stripped else ""
    return stripped.split(" #", 1)[0].strip()


def resolve_provider(raw: str) -> str:
    provider = (raw or "bedrock").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"MODEL_PROVIDER={raw!r} is not one of {', '.join(PROVIDERS)}")
    return provider


def resolve_tier_model(provider: str, tier: str, model_id_env: str = "") -> str:
    """Model for a tier: MODEL_ID_<TIER> wins, else the provider's default for that tier."""
    if tier not in TIERS:
        raise ValueError(f"unknown model tier {tier!r}; expected one of {TIERS}")
    return model_id_env or DEFAULT_TIER_MODELS[provider][tier]


def resolve_role_tier(role: str, tier_env: str = "") -> str:
    tier = (tier_env or DEFAULT_ROLE_TIERS.get(role, "standard")).strip().lower()
    if tier not in TIERS:
        raise ValueError(f"MODEL_TIER_{role.upper()}={tier!r} is not one of {TIERS}")
    return tier


def next_tier(tier: str) -> str | None:
    """One-step escalation: fast -> standard -> strong -> None."""
    i = TIERS.index(tier)
    return TIERS[i + 1] if i + 1 < len(TIERS) else None


def resolve_model_id(provider: str, model_id: str, bedrock_model_id: str = "") -> str:
    """MODEL_ID wins; BEDROCK_MODEL_ID is honoured for the bedrock provider only (legacy name);
    otherwise the per-provider default."""
    if model_id:
        return model_id
    if provider == "bedrock" and bedrock_model_id:
        return bedrock_model_id
    return DEFAULT_MODEL_IDS[provider]


@dataclass(frozen=True)
class Settings:
    aws_region: str = _env("AWS_REGION", "us-east-1")
    # Model provider: bedrock | anthropic | openai | gemini (see shared/models.py)
    model_provider: str = resolve_provider(_env("MODEL_PROVIDER", "bedrock"))
    model_id: str = resolve_model_id(
        resolve_provider(_env("MODEL_PROVIDER", "bedrock")),
        _env("MODEL_ID"),
        _env("BEDROCK_MODEL_ID"),
    )
    temperature: float = float(_env("MODEL_TEMPERATURE", "0.1"))
    max_tokens: int = int(_env("MODEL_MAX_TOKENS", "4096"))
    # On AWS, the provider API key is fetched from this Secrets Manager secret at startup.
    model_api_key_secret_arn: str = _env("MODEL_API_KEY_SECRET_ARN", "")
    guardrail_id: str = _env("BEDROCK_GUARDRAIL_ID", "")
    guardrail_version: str = _env("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    # MCP servers (streamable HTTP)
    mcp_ais_url: str = _env("MCP_AIS_URL", "http://mcp-ais:8000/mcp")
    mcp_registry_url: str = _env("MCP_REGISTRY_URL", "http://mcp-registry:8000/mcp")
    mcp_geo_url: str = _env("MCP_GEO_URL", "http://mcp-geo:8000/mcp")
    mcp_imagery_url: str = _env("MCP_IMAGERY_URL", "http://mcp-imagery:8000/mcp")
    mcp_bearer_token: str = _env(
        "MCP_BEARER_TOKEN", ""
    )  # set when fronted by AgentCore Gateway
    # Caller identity on MCP/API calls: none | aws-iam (see shared/caller_auth.py)
    tool_auth: str = _env("TOOL_AUTH", "none").strip().lower()
    # AgentCore Gateway in front of every tool server (AWS). Empty = direct per-server URLs.
    tool_gateway_url: str = _env("TOOL_GATEWAY_URL", "").strip()
    # AgentCore Harness pilot for the Tasking node; empty = the Tasking runtime over A2A.
    tasking_harness_arn: str = _env("TASKING_HARNESS_ARN", "").strip()
    # Honour configuration-bundle prompt overrides (A/B tests) only when switched on.
    bundle_override: bool = _env("BUNDLE_OVERRIDE", "false").strip().lower() == "true"
    # A2A peers (orchestrator only)
    a2a_watch_url: str = _env("A2A_WATCH_URL", "http://agent-watch:9000")
    a2a_investigator_url: str = _env(
        "A2A_INVESTIGATOR_URL", "http://agent-investigator:9000"
    )
    a2a_tasking_url: str = _env("A2A_TASKING_URL", "http://agent-tasking:9000")
    a2a_auth: str = _env("A2A_AUTH", "none")  # none | sigv4
    # Candidates the Watch agent reviews per sweep (ranked; the rest wait for the next one).
    sweep_max_candidates: int = int(_env("SWEEP_MAX_CANDIDATES", "25") or 25)
    a2a_public_url: str = _env("A2A_PUBLIC_URL", "")  # advertised in this agent's card
    a2a_port: int = int(_env("A2A_PORT", "9000"))
    # Platform
    api_url: str = _env("API_URL", "http://api:8000")
    # SSM parameter holding the internal load balancer's certificate (AWS only, A7)
    internal_ca_ssm: str = _env("INTERNAL_CA_SSM", "")
    memory_id: str = _env("AGENTCORE_MEMORY_ID", "")
    ais_mode: str = _env("AIS_MODE", "replay").strip().lower()
    sanctions_source: str = _env("SANCTIONS_SOURCE", "local").strip().lower()
    scenario_end_setting: str = _env("SCENARIO_END", "2026-09-01T08:00:00Z")

    @property
    def scenario_end(self) -> str:
        """ "Now" for every prompt: the scenario clock in replay, the wall clock when live."""
        return scenario_clock(self.scenario_end_setting, self.ais_mode)

    deploy_env: str = _env("DEPLOY_ENV", "local")
    extra: dict = field(default_factory=dict)


def scenario_clock(scenario_end: str, ais_mode: str) -> str:
    """Pure: the clock windows are measured from. Live AIS has no scenario, so the wall clock."""
    if (ais_mode or "replay").strip().lower() == "live":
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return scenario_end


settings = Settings()


def health_url(mcp_url: str) -> str:
    """The /health endpoint next to an MCP server's /mcp path. Only the trailing path is
    swapped: a naive replace also hit host names that start with `mcp` (`mcp-ais` became
    `health-ais` and every Strands agent waited 180 s and died on compose)."""
    import re

    return re.sub(r"/mcp/?$", "/health", mcp_url.rstrip())
